"""Read taps from the ADS7846 resistive touchscreen using raw Linux input events."""
import fcntl
import json
import os
import statistics
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

EVENT = struct.Struct("llHHi")  # struct input_event: timeval, type, code, value
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y = 0x00, 0x01
BTN_TOUCH = 0x14A
EVIOCGRAB = 0x40044590

CALIBRATION_FILE = Path.home() / ".config" / "wifi-meter" / "touch.json"


def find_device(name: str = "ADS7846 Touchscreen") -> str | None:
    for node in sorted(Path("/sys/class/input").glob("event*")):
        try:
            if (node / "device" / "name").read_text().strip() == name:
                return f"/dev/input/{node.name}"
        except OSError:
            continue
    return None


def start_watcher(display, on_tap, device: str | None = None):
    """Use the display's shared touch transport, or a native input device."""
    if hasattr(display, "read_touch"):
        from .kedei import XPT2046Watcher
        watcher = XPT2046Watcher(display, on_tap)
    else:
        device = device or find_device()
        if not device:
            return None
        watcher = TapWatcher(device, on_tap)
    watcher.start()
    return watcher


@dataclass
class Calibration:
    """Linear map from raw ADC readings to framebuffer pixels (unrotated screen).

    Defaults were measured on a HUGA panel with the huga35 overlay at rotate=90.
    """
    swap_xy: bool = True
    ax: float = -0.1248
    bx: float = 495.2
    ay: float = 0.08835
    by: float = 2.87

    def to_screen(self, raw_x: int, raw_y: int, size=(480, 320)) -> tuple[int, int]:
        u, v = (raw_y, raw_x) if self.swap_xy else (raw_x, raw_y)
        x = min(max(round(self.ax * u + self.bx), 0), size[0] - 1)
        y = min(max(round(self.ay * v + self.by), 0), size[1] - 1)
        return x, y

    @classmethod
    def fit(cls, points: list[tuple[tuple[int, int], tuple[int, int]]]) -> "Calibration":
        """Fit from ((screen_x, screen_y), (raw_x, raw_y)) pairs spanning both axes."""
        def line(pairs):
            # least-squares slope/intercept of screen = a * raw + b
            mean_r = statistics.fmean(r for _, r in pairs)
            mean_s = statistics.fmean(s for s, _ in pairs)
            var = sum((r - mean_r) ** 2 for _, r in pairs)
            a = sum((r - mean_r) * (s - mean_s) for s, r in pairs) / var
            return a, mean_s - a * mean_r

        # The raw axis that tracks screen x best is the one to use for x.
        def corr(axis_screen, axis_raw):
            pairs = [(s[axis_screen], r[axis_raw]) for s, r in points]
            return abs(statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs]))
        swap = corr(0, 1) > corr(0, 0)
        rx, ry = (1, 0) if swap else (0, 1)
        ax, bx = line([(s[0], r[rx]) for s, r in points])
        ay, by = line([(s[1], r[ry]) for s, r in points])
        return cls(swap, ax, bx, ay, by)

    @classmethod
    def load(cls, path: Path = CALIBRATION_FILE) -> "Calibration":
        try:
            return cls(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path = CALIBRATION_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")


EDGE_SAMPLES = 3  # samples averaged for a gesture's start and end points


def median_point(samples: list[tuple[int, int]]) -> tuple[int, int]:
    return (statistics.median_low(s[0] for s in samples),
            statistics.median_low(s[1] for s in samples))


class TapWatcher(threading.Thread):
    """Call ``on_tap(raw_x, raw_y, seconds_held, start, end)`` when a finger lifts.

    ``raw_x, raw_y`` is the median of the samples taken while it was down (resistive
    panels are noisy at first contact); ``start`` and ``end`` are raw (x, y) medians of
    the first and last few samples, so callers can tell a swipe from a tap.
    With ``grab`` the device is opened exclusively so taps don't also reach the desktop.
    """

    def __init__(self, device: str, on_tap, cooldown: float = 0.25, grab: bool = True):
        super().__init__(daemon=True)
        self.device = device
        self.on_tap = on_tap
        self.cooldown = cooldown
        self.grab = grab

    def run(self):
        while True:
            try:
                self._watch()
            except OSError as exc:  # e.g. another program holds the grab; try again
                print(f"touchscreen {self.device}: {exc}; retrying", file=sys.stderr, flush=True)
                time.sleep(5)

    def _watch(self):
        fd = os.open(self.device, os.O_RDONLY)
        try:
            if self.grab:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError:
            os.close(fd)
            raise
        x = y = None
        down = False
        samples: list[tuple[int, int]] = []
        last = pressed = 0.0
        try:
            while True:
                data = os.read(fd, EVENT.size * 64)
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, ev_type, code, value = EVENT.unpack_from(data, offset)
                    if ev_type == EV_ABS and code == ABS_X:
                        x = value
                    elif ev_type == EV_ABS and code == ABS_Y:
                        y = value
                    elif ev_type == EV_KEY and code == BTN_TOUCH:
                        down = value == 1
                        now = time.monotonic()
                        if down:
                            samples, pressed = [], now
                        elif samples and now - last >= self.cooldown:
                            last = now
                            self.on_tap(*median_point(samples), now - pressed,
                                        median_point(samples[:EDGE_SAMPLES]),
                                        median_point(samples[-EDGE_SAMPLES:]))
                    elif ev_type == EV_SYN and code == SYN_REPORT and down and x is not None:
                        samples.append((x, y))
        finally:
            os.close(fd)
