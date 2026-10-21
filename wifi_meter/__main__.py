"""Wi-Fi signal strength meter for a 480x320 framebuffer or KeDei V6.2 LCD.

    python3 -m wifi_meter                  # run on /dev/fb0
    python3 -m wifi_meter --demo           # run on /dev/fb0 with sample data
    python3 -m wifi_meter --preview out.png [--demo] [--first N]

Two views: the Wi-Fi meter and Server Status (the webserver's "Server status" tile,
from MQTT). Swipe left or right to switch; the dots in the header show which is up.

Touch: tap ▲ / ▼ on the right edge to page through the Wi-Fi list; press and hold
anywhere else for about a second to rotate the display 180°. Alerts from the
LAN MQTT bus cover the view until tapped or swiped away. An "identify" command (the
webserver's Raspberry Pis page) fills the screen with the Pi's name for 30 s or until tapped.
"""
import argparse
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from PIL import Image

from . import bus, scanner, server_view, touch
from .render import HEIGHT, ROW_TOP, STAMP_FORMAT, STRIP_X, WIDTH, render, visible_rows
from .server_view import render_server

DEMO = [
    scanner.Network("HomeNet", "", 6, 2437, 82, "WPA2", True, 2),
    scanner.Network("Neighbor 5G", "", 44, 5220, 64, "WPA2 WPA3", False),
    scanner.Network("xfinitywifi", "", 1, 2412, 51, "", False),
    scanner.Network("", "", 11, 2462, 38, "WPA2", False),
    scanner.Network("A very long network name that will not fit", "", 149, 5745, 27, "WPA3", False),
    scanner.Network("DIRECT-HP-Printer", "", 6, 2437, 12, "WPA2", False),
] + [
    scanner.Network(f"Demo-{i:02}", "", 1 + i % 11, 2412 + 5 * (i % 11), 60 - 4 * i, "WPA2", False)
    for i in range(1, 11)
]
DEMO.sort(key=lambda n: -n.signal)

FLIP_FILE = Path.home() / ".config" / "wifi-meter" / "flipped"
VIEW_FILE = Path.home() / ".config" / "wifi-meter" / "view"
VIEWS = ("wifi", "server")  # swipe left for the next, right for the previous
# The panel's touch layer reads compressed near its edges (the top ~40 px all register
# around y=42, the right edge reads ~25 px short), so the ▲/▼ zone is wider than the drawn
# strip and rotation uses a long press anywhere else rather than a title-bar tap.
STRIP_HIT_X = STRIP_X - 30
LONG_PRESS = 0.8  # seconds
SWIPE_MIN = 100   # horizontal travel in pixels, at least 1.5x the vertical travel


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _sigterm(*_):
    raise SystemExit(0)


class ScanFeed(threading.Thread):
    """Scan in the background and post ("scan", networks or None, error) events."""

    def __init__(self, scan, interval: float, events: queue.Queue):
        super().__init__(daemon=True)
        self._scan = scan
        self._interval = interval
        self._events = events

    def run(self):
        while True:
            try:
                self._events.put(("scan", self._scan(), ""))
            except Exception as exc:  # keep scanning whatever goes wrong
                log(f"scan failed: {exc}")
                self._events.put(("scan", None, "scan failed"))
            time.sleep(self._interval)


class Orientation:
    """180° flip toggled from the title bar and remembered across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.flipped = path.exists()

    def toggle(self) -> None:
        self.flipped = not self.flipped
        try:
            if self.flipped:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch()
            else:
                self.path.unlink(missing_ok=True)
        except OSError as exc:
            log(f"could not save orientation: {exc}")


class AlertBoard:
    """Alerts received from the bus, most severe then newest first.

    Every alert is kept and ``wants`` (which follows the hub's "Show alerts" setting)
    filters them when drawing, so changing the setting takes effect at once, both ways.
    Dismissing hides an alert on this screen only; it shows again if the source
    re-raises it (new ``since``) or after a restart.
    """

    def __init__(self, wants):
        self.wants = wants
        self.alerts: dict[str, bus.Alert] = {}
        self.dismissed: set[tuple[str, float]] = set()

    def update(self, topic: str, alert: "bus.Alert | None") -> None:
        if alert is None:
            self.alerts.pop(topic, None)
        else:
            self.alerts[topic] = alert

    def expire(self) -> bool:
        stale = [t for t, a in self.alerts.items() if a.expired()]
        for topic in stale:
            del self.alerts[topic]
        return bool(stale)

    def visible(self) -> list:
        shown = [a for a in self.alerts.values()
                 if (a.topic, a.since) not in self.dismissed and not a.expired() and self.wants(a)]
        return sorted(shown, key=lambda a: (-a.rank, -a.since))

    def dismiss(self, alert) -> None:
        self.dismissed.add((alert.topic, alert.since))


def start_bus(events: queue.Queue):
    config = bus.load_config()
    if not config:
        log(f"MQTT disabled: no device login at {bus.CREDENTIALS_FILE} "
            "(deploy the pi-health agent from ../webserver first)")
        return None, None
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        log("MQTT disabled: python3-paho-mqtt is not installed (run deploy.ps1 -Install)")
        return None, None
    link = bus.Bus(config, on_alert=lambda topic, alert: events.put(("alert", topic, alert)),
                   on_state=lambda connected: events.put(("bus", connected)),
                   on_command=lambda command, data: events.put(("command", command, data)),
                   on_server=lambda kind, data: events.put(("server", kind, data)))
    link.start()
    return link, AlertBoard(link.wants)


def load_view() -> int:
    try:
        return VIEWS.index(VIEW_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def save_view(index: int) -> None:
    try:
        VIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
        VIEW_FILE.write_text(VIEWS[index] + "\n")
    except OSError as exc:
        log(f"could not save view: {exc}")


def run(args) -> None:
    from .display import open_display

    fb = open_display(args.device, args.backend, int(args.spi_speed * 1e6))
    signal.signal(signal.SIGTERM, _sigterm)
    events: queue.Queue = queue.Queue()

    status = ""
    if args.demo:
        scan = lambda: DEMO
    else:
        allowed, err = scanner.can_rescan()
        if not allowed:
            status = "rescan not permitted — cached results"
            log(f"warning: cannot trigger Wi-Fi rescan ({err}); install the polkit rule")
        scan = lambda: scanner.scan(rescan=allowed, group=not args.all_aps)

    orientation = Orientation(FLIP_FILE)
    calibration = touch.Calibration.load()
    tap_watcher = None
    if not args.no_touch:
        tap_watcher = touch.start_watcher(
            fb, lambda *touched: events.put(("tap", *touched)), args.touch_device
        )
        if tap_watcher:
            if not touch.CALIBRATION_FILE.exists():
                log("warning: touchscreen not calibrated; run python3 -m wifi_meter.calibrate")
        else:
            log("warning: touchscreen not found; touch controls disabled")

    link, board = (None, None) if args.no_mqtt else start_bus(events)
    bus_connected = False if link else None

    ScanFeed(scan, args.interval, events).start()
    rows = visible_rows(fb.height)
    networks = updated = None
    first = 0
    scan_status = status
    alert = None
    identify = None  # {text, subtitle, until} while the identify screen is up
    view = load_view()
    server = {"summary": None, "presence": None}  # the server view's data, from the bus
    minute = int(time.time() // 60)
    dirty = True

    def to_screen(raw):
        x, y = calibration.to_screen(*raw, fb.size)
        return (fb.width - 1 - x, fb.height - 1 - y) if orientation.flipped else (x, y)

    try:
        while True:
            if identify and time.time() >= identify["until"]:
                identify = None
                dirty = True
            if dirty:
                alerts = board.visible() if board else []
                alert = alerts[0] if alerts else None
                dots = (view, len(VIEWS))
                if VIEWS[view] == "server":
                    image = render_server(server, fb.size, bus_connected, dots,
                                          alert, (1, len(alerts)), identify)
                else:
                    image = render(networks, first, fb.size, scan_status, updated,
                                   alert, (1, len(alerts)), bus_connected, identify, dots)
                fb.show(image.transpose(Image.Transpose.ROTATE_180) if orientation.flipped else image)
                dirty = False

            try:
                event = events.get(timeout=1)  # timeout keeps SIGTERM responsive
            except queue.Empty:
                # The identify countdown redraws every second; the server view's
                # "updated N ago" and uptime once a minute.
                now_minute = int(time.time() // 60)
                dirty = (bool(board and board.expire()) or identify is not None
                         or (VIEWS[view] == "server" and now_minute != minute))
                minute = now_minute
                continue
            dirty = True
            kind = event[0]
            if kind == "scan":
                _, found, error = event
                before = (networks, scan_status, stamp(updated))
                if found is not None:
                    networks, updated = found, time.time()
                    if link:
                        link.publish_reading("wifi", wifi_reading(networks))
                scan_status = error or status
                # Redraw only when something on screen changes: the list, the status, or
                # the header's HH:MM (a scan with the same results changes nothing).
                dirty = VIEWS[view] == "wifi" and (networks, scan_status, stamp(updated)) != before
            elif kind == "alert":
                board.update(event[1], event[2])
            elif kind == "bus":
                bus_connected = event[1]
            elif kind == "server":
                server[event[1]] = event[2]
                dirty = VIEWS[view] == "server"
            elif kind == "command":
                command, data = event[1], event[2]
                if command == "identify":
                    seconds = min(max(float(data.get("seconds") or 30), 1), 300)
                    identify = {"text": str(data.get("text") or bus.device_name()),
                                "subtitle": str(data.get("subtitle") or ""), "until": time.time() + seconds}
                    log(f"identify for {seconds:.0f} s (from {data.get('from', '?')})")
                elif command == "settings":
                    pass  # the hub's "Show alerts" setting changed: redraw with the new filter
                else:
                    log(f"ignoring unknown command {command!r}")
                    dirty = False
            elif kind == "tap" and identify:
                identify = None   # any tap or swipe dismisses the identify screen first
                log("identify dismissed by tap")
            elif kind == "tap":
                _, raw_x, raw_y, held, start, end = event
                x, y = to_screen((raw_x, raw_y))
                (sx, sy), (ex, ey) = to_screen(start), to_screen(end)
                swipe = abs(ex - sx) >= SWIPE_MIN and abs(ex - sx) >= 1.5 * abs(ey - sy)
                wifi_view = VIEWS[view] == "wifi"
                if alert and (swipe or held < LONG_PRESS):
                    action = f"dismiss {alert.topic}"   # alerts cover every view until tapped or swiped away
                    board.dismiss(alert)
                elif swipe:
                    view = (view + (1 if ex < sx else -1)) % len(VIEWS)
                    save_view(view)
                    action = f"swipe {'left' if ex < sx else 'right'} -> {VIEWS[view]}"
                elif held >= LONG_PRESS and (alert or not wifi_view or x < STRIP_HIT_X):
                    action = "flip"
                    orientation.toggle()
                elif wifi_view and x >= STRIP_HIT_X:
                    page = rows if y >= (ROW_TOP + fb.height) // 2 else -rows
                    action = "down" if page > 0 else "up"
                    first = first + page
                else:
                    action = "none"
                    dirty = False  # taps on the list itself do nothing
                log(f"touch raw ({raw_x}, {raw_y}) -> ({x}, {y}) from ({sx}, {sy}) to ({ex}, {ey}) "
                    f"held {held:.2f}s {action}")
            first = min(max(0, first), max(0, len(networks or ()) - rows))
    except KeyboardInterrupt:
        pass
    finally:
        if tap_watcher and hasattr(tap_watcher, "stop"):
            tap_watcher.stop()
            tap_watcher.join()
        if link:
            link.stop()
        try:
            fb.clear()
        finally:
            fb.close()


def stamp(updated: float | None) -> str:
    """The scan time as the Wi-Fi header shows it."""
    return time.strftime(STAMP_FORMAT, time.localtime(updated)) if updated else ""


def wifi_reading(networks: list) -> dict:
    current = next((n for n in networks if n.in_use), None)
    return {
        "ssid": current.ssid if current else None,
        "signal": current.signal if current else None,
        "channel": current.channel if current else None,
        "networks": len(networks),
        "since": int(time.time()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="wifi_meter", description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="/dev/fb0", help="framebuffer device")
    parser.add_argument("--backend", choices=("auto", "framebuffer", "kedei"), default="auto",
                        help="display driver (default: existing framebuffer, then configured KeDei)")
    parser.add_argument("--spi-speed", type=float, default=4,
                        help="KeDei LCD SPI clock in MHz (default: 4)")
    parser.add_argument("--interval", type=float, default=10, help="seconds between scans")
    parser.add_argument("--all-aps", action="store_true",
                        help="list every access point instead of one row per SSID")
    parser.add_argument("--no-touch", action="store_true", help="disable touch controls")
    parser.add_argument("--touch-device", metavar="EVENT",
                        help="touchscreen input device (default: find the ADS7846)")
    parser.add_argument("--no-mqtt", action="store_true", help="don't join the MQTT alert bus")
    parser.add_argument("--preview", metavar="PNG", help="render one frame to a PNG and exit")
    parser.add_argument("--first", type=int, default=0, help="first row shown in --preview")
    parser.add_argument("--preview-alert", choices=list(bus.LEVELS),
                        help="draw a sample alert of this level in --preview")
    parser.add_argument("--preview-identify", metavar="NAME[:SUBTITLE]",
                        help="draw the identify screen in --preview")
    parser.add_argument("--view", choices=VIEWS, default="wifi", help="which view --preview draws")
    parser.add_argument("--demo", action="store_true", help="use sample data instead of scanning")
    args = parser.parse_args()

    if args.preview and args.view == "server":
        # --demo: a sample summary with issues; otherwise the "waiting" screen.
        server = {"summary": server_view.DEMO if args.demo else None,
                  "presence": "online" if args.demo else None}
        render_server(server, (WIDTH, HEIGHT), True, (VIEWS.index("server"), len(VIEWS))).save(args.preview)
        print(f"wrote {args.preview} (server view)")
        return
    if args.preview:
        if args.demo:
            networks = DEMO
        else:
            networks = scanner.scan(rescan=scanner.can_rescan()[0], group=not args.all_aps)
        alert = None
        if args.preview_alert:
            alert = bus.Alert("alerts/manual/doorbell", "manual", "doorbell", args.preview_alert,
                              "Someone is at the front door",
                              "Doorbell pressed. The camera snapshot is on the tiles page; "
                              "this message is long enough to wrap onto several lines.",
                              time.time())
        identify = None
        if args.preview_identify:
            text, _, subtitle = args.preview_identify.partition(":")
            identify = {"text": text, "subtitle": subtitle, "until": time.time() + 27}
        render(networks, args.first, (WIDTH, HEIGHT), updated=time.time(), alert=alert,
               alert_index=(1, 2), bus_connected=True, identify=identify,
               view=(0, len(VIEWS))).save(args.preview)
        print(f"wrote {args.preview} ({len(networks)} networks)")
        return
    run(args)


if __name__ == "__main__":
    main()
