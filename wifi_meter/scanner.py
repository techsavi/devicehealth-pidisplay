"""Scan nearby Wi-Fi networks through NetworkManager's nmcli."""
import re
import subprocess
from dataclasses import dataclass

FIELDS = "IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY"
# nmcli -t escapes ':' and '\' inside values with a backslash.
_SPLIT = re.compile(r"(?<!\\):")


@dataclass
class Network:
    ssid: str
    bssid: str
    channel: int
    freq_mhz: int
    signal: int  # 0-100 %, as reported by NetworkManager
    security: str
    in_use: bool
    ap_count: int = 1

    @property
    def hidden(self) -> bool:
        return not self.ssid

    @property
    def band(self) -> str:
        if self.freq_mhz >= 5925:
            return "6G"
        if self.freq_mhz >= 4900:
            return "5G"
        return "2.4G"

    @property
    def secured(self) -> bool:
        return self.security not in ("", "--")


class ScanError(RuntimeError):
    pass


def _unescape(value: str) -> str:
    return value.replace("\\:", ":").replace("\\\\", "\\")


def _int(value: str) -> int:
    match = re.match(r"\d+", value)
    return int(match.group()) if match else 0


def parse(output: str) -> list[Network]:
    networks = []
    for line in output.splitlines():
        parts = [_unescape(p) for p in _SPLIT.split(line)]
        if len(parts) != 7:
            continue
        in_use, ssid, bssid, chan, freq, signal, security = parts
        networks.append(Network(
            ssid=ssid,
            bssid=bssid,
            channel=_int(chan),
            freq_mhz=_int(freq),
            signal=_int(signal),
            security=security,
            in_use=in_use.strip() == "*",
        ))
    return networks


def group_by_ssid(networks: list[Network]) -> list[Network]:
    """Collapse access points sharing an SSID into the strongest one.

    Hidden networks are never merged since they cannot be told apart by name.
    """
    best: dict[str, Network] = {}
    singles = []
    for net in networks:
        if net.hidden:
            singles.append(net)
            continue
        current = best.get(net.ssid)
        if current is None:
            best[net.ssid] = net
            continue
        count = current.ap_count + 1
        in_use = current.in_use or net.in_use
        if net.signal > current.signal:
            current = best[net.ssid] = net
        current.ap_count = count
        current.in_use = in_use
    return list(best.values()) + singles


def can_rescan() -> tuple[bool, str]:
    """Return whether this user may ask NetworkManager for a fresh scan."""
    result = subprocess.run(["nmcli", "device", "wifi", "rescan"],
                            capture_output=True, text=True, timeout=15)
    return result.returncode == 0, result.stderr.strip()


def scan(rescan: bool = True, group: bool = True) -> list[Network]:
    cmd = ["nmcli", "-t", "-f", FIELDS, "device", "wifi", "list",
           "--rescan", "yes" if rescan else "no"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScanError(str(exc)) from exc
    if result.returncode != 0:
        raise ScanError(result.stderr.strip() or f"nmcli exited {result.returncode}")
    networks = parse(result.stdout)
    if group:
        networks = group_by_ssid(networks)
    networks.sort(key=lambda n: (-n.signal, n.ssid.lower()))
    return networks
