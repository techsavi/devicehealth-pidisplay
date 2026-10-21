"""Join the webserver project's MQTT alert bus over the LAN.

Conventions (see ../webserver README, "MQTT"):
  alerts/<source>/<id>   retained JSON {level, title, message, since, expires?};
                         an empty retained payload clears the alert
  status/<name>          retained "online"/"offline"; owned by the pi-health agent
  devices/<name>/...     this device's readings
  cmd/<name>/<command>   commands for this device (JSON, not retained), e.g. "identify"
                         from the webserver's Raspberry Pis page
  devices/statuspanel/summary   the "Server status" tile's data, for the server view

The meter joins as the device itself: <name> is the Pi's hostname, read at startup,
and the login is the device's shared one from the pi-health agent's config (written
by ../webserver/agents/pi/deploy.mjs). The broker's ACL only lets a login write
under its own name, so after renaming the Pi, rerun deploy.mjs for the new name.
"""
import ipaddress
import json
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

CREDENTIALS_FILE = Path.home() / ".config" / "pi-health" / "config.json"
# The webserver's "Server status" tile data, published by ../statuspanel's poller after every
# poll (retained), and that poller's presence ("online", or "offline" from its last will).
SERVER_SUMMARY = "devices/statuspanel/summary"
SERVER_PRESENCE = "status/statuspanel"
SETTINGS_FILE = Path.home() / ".config" / "wifi-meter" / "mqtt.json"  # optional overrides
LEVELS = {"info": 0, "warning": 1, "critical": 2}
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def log(message: str) -> None:
    print(f"mqtt: {message}", file=sys.stderr, flush=True)


@dataclass
class Alert:
    topic: str
    source: str
    id: str
    level: str
    title: str
    message: str
    since: float
    expires: float | None = None

    @property
    def rank(self) -> int:
        return LEVELS.get(self.level, 0)

    def expired(self, now: float | None = None) -> bool:
        return bool(self.expires) and self.expires <= (now or time.time())


def parse_alert(topic: str, payload: bytes) -> Alert | None:
    """Return the alert on ``topic``, or None when the payload clears it."""
    if not payload:
        return None
    _, source, alert_id = topic.split("/", 2)
    data = json.loads(payload)
    level = data.get("level") if data.get("level") in LEVELS else "info"
    return Alert(topic, source, alert_id, level,
                 str(data.get("title") or ""), str(data.get("message") or ""),
                 float(data.get("since") or time.time()),
                 float(data["expires"]) if data.get("expires") else None)


def device_name() -> str:
    return socket.gethostname().split(".")[0]


def load_config() -> dict | None:
    """Device login from the pi-health config, plus optional meter settings.

    Returns None when there is no login on this Pi.
    """
    try:
        config = json.loads(CREDENTIALS_FILE.read_text())
    except FileNotFoundError:
        return None
    try:
        config.update(json.loads(SETTINGS_FILE.read_text()))
    except FileNotFoundError:
        pass
    config["name"] = device_name()
    if config.get("username") != config["name"]:
        log(f"warning: login {config.get('username')!r} doesn't match hostname {config['name']!r}; "
            "the broker will refuse our publishes. Rerun webserver agents/pi/deploy.mjs for this name")
    config.setdefault("port", 1883)
    config.setdefault("min_level", "warning")
    config.setdefault("all_levels_from", ["manual", "tabletpanel", config["name"]])
    return config


def lan_address(host: str, port: int) -> str:
    """Resolve ``host`` and insist it is on the local network (never internet or tailnet)."""
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback) and ip not in TAILNET:
            return str(ip)
    raise ValueError(f"{host} does not resolve to a LAN address; refusing to connect")


def alert_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text.lower()).strip("-")[:64] or "alert"


def _client(config: dict, role: str):
    """A client with id ``<device>-<role>``, beside pi-health's ``<device>-health``."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"{config['name']}-{role}", protocol=mqtt.MQTTv5)
    client.username_pw_set(config["username"], config["password"])
    return client


def _expiry_properties(seconds: int | None):
    if not seconds:
        return None
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    props = Properties(PacketTypes.PUBLISH)
    props.MessageExpiryInterval = int(seconds)
    return props


def alert_payload(level: str, title: str, message: str = "", expires_in: int | None = None) -> str:
    since = int(time.time())
    payload = {"level": level, "title": title or None, "message": message or None,
               "since": since, "expires": since + expires_in if expires_in else None}
    return json.dumps({k: v for k, v in payload.items() if v is not None})


class Bus:
    """Long-lived connection for the meter: alert subscription and readings.

    The meter's own presence is ``devices/<name>/display`` ("online", last will
    "offline"); ``status/<name>`` belongs to pi-health, which reports the Pi itself.
    ``on_alert(topic, Alert | None)``, ``on_command(command, data)``,
    ``on_server("summary" | "presence", data)`` and ``on_state(connected)`` are called
    from paho's network thread.
    """

    def __init__(self, config: dict, on_alert, on_state=lambda connected: None,
                 on_command=lambda command, data: None, on_server=lambda kind, data: None):
        self.config = config
        self.on_alert = on_alert
        self.on_state = on_state
        self.on_command = on_command
        self.on_server = on_server
        self.connected = False
        self._client = None
        self._presence = f"devices/{config['name']}/display"
        self._registry = f"registry/devices/{config['name']}"
        # The hub's "Show alerts" setting for this device (registry/devices/<name> "alerts"):
        # None = the default below, else "all", "warning", "critical" or "off".
        self.alerts_setting: str | None = None

    def wants(self, alert: Alert) -> bool:
        setting = self.alerts_setting
        if setting == "off":
            return False
        if setting in ("all", "warning", "critical"):
            return alert.rank >= LEVELS.get("info" if setting == "all" else setting, 1)
        return (alert.source in self.config["all_levels_from"]
                or alert.rank >= LEVELS.get(self.config["min_level"], 1))

    def start(self) -> None:
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self) -> None:
        while True:  # DNS may not be up yet at boot
            try:
                address = lan_address(self.config["host"], self.config["port"])
                break
            except ValueError as exc:
                log(str(exc))
                return
            except OSError as exc:
                log(f"cannot resolve {self.config['host']}: {exc}; retrying")
                time.sleep(10)
        client = self._client = _client(self.config, "display")
        client.will_set(self._presence, "offline", qos=1, retain=True)
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(address, self.config["port"], keepalive=30)
        client.loop_start()
        log(f"connecting to {self.config['host']} ({address}:{self.config['port']}) "
            f"as {self.config['name']}-display")

    def stop(self) -> None:
        if not self._client:
            return
        if self.connected:
            self._client.publish(self._presence, "offline", qos=1, retain=True).wait_for_publish(2)
        self._client.disconnect()
        self._client.loop_stop()

    def publish_reading(self, reading: str, data: dict) -> None:
        if self.connected:
            self._client.publish(f"devices/{self.config['name']}/{reading}", json.dumps(data),
                                 qos=0, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log(f"connect refused: {reason_code}")
            return
        client.publish(self._presence, "online", qos=1, retain=True)
        client.subscribe("alerts/+/+", qos=1)
        client.subscribe(f"cmd/{self.config['name']}/+", qos=1)
        client.subscribe(self._registry, qos=1)
        client.subscribe([(SERVER_SUMMARY, 1), (SERVER_PRESENCE, 1)])
        self.connected = True
        self.on_state(True)
        log("connected")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.connected:
            log(f"disconnected: {reason_code}")
        self.connected = False
        self.on_state(False)

    def _on_message(self, client, userdata, message):
        if message.topic == SERVER_SUMMARY:
            try:
                summary = json.loads(message.payload) if message.payload else None
            except ValueError:
                log(f"ignoring malformed {SERVER_SUMMARY}")
                return
            self.on_server("summary", summary if isinstance(summary, dict) else None)
            return
        if message.topic == SERVER_PRESENCE:
            self.on_server("presence", message.payload.decode(errors="replace") or None)
            return
        if message.topic == self._registry:
            try:
                entry = json.loads(message.payload) if message.payload else {}
            except ValueError:
                entry = {}
            setting = entry.get("alerts") if isinstance(entry, dict) else None
            if setting != self.alerts_setting:
                self.alerts_setting = setting
                log(f"alerts on this display: {setting or 'default'}")
                self.on_command("settings", {"alerts": setting})   # redraw with the new filter
            return
        if message.topic.startswith("cmd/"):
            try:
                data = json.loads(message.payload or b"{}")
            except ValueError:
                log(f"ignoring malformed command on {message.topic}")
                return
            self.on_command(message.topic.rsplit("/", 1)[1], data if isinstance(data, dict) else {})
            return
        try:
            alert = parse_alert(message.topic, message.payload)
        except (ValueError, KeyError, TypeError) as exc:
            log(f"ignoring malformed alert on {message.topic}: {exc}")
            return
        self.on_alert(message.topic, alert)


def send_alert(config: dict, alert_id: str, level: str = "info", title: str = "",
               message: str = "", expires_in: int | None = None, clear: bool = False) -> str:
    """Publish (or clear) retained ``alerts/<device>/<alert_id>`` and wait for the broker."""
    topic = f"alerts/{config['name']}/{alert_id}"
    client = _client(config, "notify")
    client.connect(lan_address(config["host"], config["port"]), config["port"], keepalive=10)
    client.loop_start()
    try:
        payload = "" if clear else alert_payload(level, title, message, expires_in)
        info = client.publish(topic, payload, qos=1, retain=True,
                              properties=None if clear else _expiry_properties(expires_in))
        info.wait_for_publish(5)
        if not info.is_published():
            raise RuntimeError("broker did not acknowledge the alert")
    finally:
        client.disconnect()
        client.loop_stop()
    return topic
