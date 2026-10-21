"""KeDei 3.5-inch SPI TFT V6.2 with one shared LCD/touch transport.

The current-kernel ``spi0-1cs,cs0_pin=7`` overlay maps logical spidev0.0 to
physical LCD CE1. The native helper exclusively owns GPIO8 (touch CS and LCD
latch) and GPIO25, using GPIO character-device v2 rather than /dev/gpiomem.
LCD frames follow the exact V6.2 protocol; every frame is latched using GPIO8.
Touch reads use SPI_NO_CS while LCD CS remains high, under the same mutex.

Protocol: https://github.com/lzto/RaspberryPi_KeDei_35_lcd_v62

    python3 -m wifi_meter.kedei                 # colour card and timing
    python3 -m wifi_meter.kedei --touch 90      # then print taps for 90 seconds
"""
import ctypes
import errno
import math
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

from .display import changed_rects, to_rgb565
from .touch import EDGE_SAMPLES, median_point

WIDTH, HEIGHT = 480, 320
LCD_DEV = "/dev/spidev0.0"
DEFAULT_SPEED_HZ = 4_000_000
# Native status 2 means PENIRQ remains low but its ADC sample was rejected.
# It must not be interpreted as the release of a finger already being tracked.
TOUCH_DOWN_INVALID = object()
_NATIVE_SOURCE = Path(__file__).with_name("_kedei_transport.c")
_NATIVE_LIBRARY = Path(__file__).with_name("_kedei_transport.so")
_BUILD_LOCK = threading.Lock()


def _load_native() -> ctypes.CDLL:
    """Build only when needed; publish a complete shared library atomically."""
    with _BUILD_LOCK:
        try:
            source_modified = _NATIVE_SOURCE.stat().st_mtime_ns
        except OSError as exc:
            raise RuntimeError(f"KeDei transport source is unavailable: {_NATIVE_SOURCE}") from exc
        try:
            needs_build = _NATIVE_LIBRARY.stat().st_mtime_ns < source_modified
        except FileNotFoundError:
            needs_build = True
        if needs_build:
            compiler = shutil.which("cc")
            if compiler is None:
                raise RuntimeError(
                    "KeDei needs the C compiler 'cc' to build its transport on first use. "
                    "Provide cc or a prebuilt _kedei_transport.so for this Pi."
                )
            fd, temporary_name = tempfile.mkstemp(
                prefix=".kedei-transport-", suffix=".so", dir=_NATIVE_LIBRARY.parent
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                command = [compiler, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
                           "-Werror", "-pthread", str(_NATIVE_SOURCE), "-o", str(temporary)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                if result.returncode:
                    diagnostic = (result.stderr or result.stdout).strip()
                    raise RuntimeError(f"Building the KeDei transport failed: {diagnostic}")
                os.replace(temporary, _NATIVE_LIBRARY)
            finally:
                temporary.unlink(missing_ok=True)
        library = ctypes.CDLL(str(_NATIVE_LIBRARY), use_errno=True)
    library.kedei_open.argtypes = [ctypes.c_uint32]
    library.kedei_open.restype = ctypes.c_void_p
    library.kedei_write_frames.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_size_t
    ]
    library.kedei_write_frames.restype = ctypes.c_int
    library.kedei_read_touch.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_int)] * 4
    library.kedei_read_touch.restype = ctypes.c_int
    library.kedei_close.argtypes = [ctypes.c_void_p]
    library.kedei_close.restype = ctypes.c_int
    return library


def _native_error(operation: str) -> OSError:
    error = ctypes.get_errno() or errno.EIO
    return OSError(error, f"{operation}: {os.strerror(error)}")


def available() -> bool:
    """Require the live one-CS mapping, rather than two unrelated device nodes."""
    try:
        if not stat.S_ISCHR(os.stat(LCD_DEV).st_mode):
            return False
        properties = (
            Path("/sys/class/spi_master/spi0/device/of_node/cs-gpios"),
            Path("/proc/device-tree/soc/spi@7e204000/cs-gpios"),
        )
        mapping = None
        for path in properties:
            try:
                mapping = path.read_bytes()
                break
            except FileNotFoundError:
                continue
        if mapping is None or len(mapping) != 12:
            return False
        controller, gpio, flags = struct.unpack(">III", mapping)
        if not controller or gpio != 7 or flags != 1:
            return False
        devices = Path("/sys/bus/spi/devices")
        if sorted(node.name for node in devices.iterdir() if node.name.startswith("spi0.")) != ["spi0.0"]:
            return False
        return (devices / "spi0.0" / "driver").resolve(strict=True).name == "spidev"
    except OSError:
        return False


# MADCTL values for the panel's native orientations; 0 is landscape 480x320.
MADCTL = {0: 0b01101010, 180: 0b10101010}

INIT = [  # (command, parameters, delay ms after)
    (0x00, [], 10), (0xFF, [], 0), (0xFF, [], 10),
    (0xFF, [], 0), (0xFF, [], 0), (0xFF, [], 0), (0xFF, [], 15),
    (0x11, [], 150),  # sleep out
    (0xB0, [0x00], 0),
    (0xB3, [0x02, 0x00, 0x00, 0x00], 0),
    (0xB9, [0x01, 0x00, 0x0F, 0x0F], 0),
    (0xC0, [0x13, 0x3B, 0x00, 0x02, 0x00, 0x01, 0x00, 0x43], 0),
    (0xC1, [0x08, 0x0F, 0x08, 0x08], 0),
    (0xC4, [0x11, 0x07, 0x03, 0x04], 0),
    (0xC6, [0x00], 0),
    (0xC8, [0x03, 0x03, 0x13, 0x5C, 0x03, 0x07, 0x14, 0x08, 0x00, 0x21,
            0x08, 0x14, 0x07, 0x53, 0x0C, 0x13, 0x03, 0x03, 0x21, 0x00], 0),
    (0x35, [0x00], 0),
    (0x36, [0x60], 0),
    (0x3A, [0x55], 0),  # 16-bit pixels
    (0x44, [0x00, 0x01], 0),
    (0xD0, [0x07, 0x07, 0x1D, 0x03], 0),
    (0xD1, [0x03, 0x30, 0x10], 0),
    (0xD2, [0x03, 0x14, 0x04], 0),
    (0x29, [], 30),  # display on
    (0xB4, [0x00], 0),  # exact V6.2 initialization before final MADCTL
]


class KeDeiDisplay:
    """Framebuffer-compatible LCD; changed rows share one native LCD/touch owner."""

    def __init__(self, device: str = LCD_DEV, speed_hz: int = DEFAULT_SPEED_HZ, rotation: int = 0):
        if device != LCD_DEV:
            raise ValueError(f"KeDei V6.2 shared transport uses {LCD_DEV}")
        if rotation not in MADCTL:
            raise ValueError("KeDei landscape rotation must be 0 or 180 degrees")
        if not isinstance(speed_hz, int) or not 10_000 <= speed_hz <= 32_000_000:
            raise ValueError("SPI speed must be an integer from 10000 to 32000000 Hz")
        if not available():
            raise OSError(errno.ENODEV,
                          "KeDei needs the live spi0-1cs,cs0_pin=7 mapping and /dev/spidev0.0")
        self.width, self.height = WIDTH, HEIGHT
        self.speed_hz = speed_hz
        self._lock = threading.RLock()
        self._closing = False
        self._handle = None
        self._watchers: set[XPT2046Watcher] = set()
        self._last: np.ndarray | None = None
        self._native = _load_native()
        ctypes.set_errno(0)
        self._handle = self._native.kedei_open(speed_hz)
        if not self._handle:
            raise _native_error("Opening the shared KeDei LCD/touch transport")
        try:
            self._reset()
            for cmd, params, delay in INIT:
                self._send(self._command(cmd, params))
                if delay:
                    time.sleep(delay / 1000)
            self._send(self._command(0x36, [MADCTL[rotation]]))
        except BaseException:
            try:
                self.close()
            except OSError as exc:
                print(f"KeDei cleanup after initialization failed: {exc}", file=sys.stderr, flush=True)
            raise

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closing or self._handle is None

    def _require_open(self) -> None:
        if self._closing or self._handle is None:
            raise RuntimeError("KeDei display is closed")

    def _register_watcher(self, watcher) -> None:
        with self._lock:
            self._require_open()
            self._watchers.add(watcher)

    def _unregister_watcher(self, watcher) -> None:
        with self._lock:
            self._watchers.discard(watcher)

    @staticmethod
    def _command(cmd: int, params=()) -> np.ndarray:
        frames = np.zeros((1 + len(params), 3), np.uint8)
        frames[0] = (0x11, 0x00, cmd)
        frames[1:, 0] = 0x15
        frames[1:, 2] = params
        return frames

    def _send(self, frames: np.ndarray, frame_len: int = 3) -> None:
        frames = np.ascontiguousarray(frames, np.uint8)
        if frame_len not in (3, 4) or frames.ndim != 2 or frames.shape[1] != frame_len:
            raise ValueError("KeDei frames must have shape (count, 3) or (count, 4)")
        with self._lock:
            self._require_open()
            if not len(frames):
                return
            ctypes.set_errno(0)
            result = self._native.kedei_write_frames(
                self._handle, frames.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
                frame_len, len(frames)
            )
            if result < 0:
                raise _native_error("Writing KeDei LCD frames")

    def _reset(self) -> None:
        for word, delay in ((b"\x00\x01\x00\x00", 50),
                            (b"\x00\x00\x00\x00", 100),
                            (b"\x00\x01\x00\x00", 50)):
            self._send(np.frombuffer(word, np.uint8).reshape(1, 4), frame_len=4)
            time.sleep(delay / 1000)

    def _window(self, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
        return np.concatenate([
            self._command(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]),
            self._command(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]),
            self._command(0x2C),
        ])

    def _write_rect(self, region: np.ndarray, x: int, y: int) -> None:
        """Write ``region`` (RGB565 rows) with its top-left at (x, y); the window and
        pixels are one native call. Every pixel costs a latched SPI frame, so callers
        send only what changed."""
        rows, cols = region.shape
        flat = np.ascontiguousarray(region).reshape(-1)
        frames = np.empty((len(flat), 3), np.uint8)
        frames[:, 0] = 0x15
        frames[:, 1] = flat >> 8
        frames[:, 2] = flat & 0xFF
        try:
            self._send(np.concatenate([self._window(x, y, x + cols - 1, y + rows - 1), frames]))
        except BaseException:
            # A failed native batch may have changed only part of the LCD.
            self._last = None
            raise

    def blit(self, pixels: np.ndarray, y: int = 0) -> None:
        """Write full-width RGB565 rows starting at screen row ``y``."""
        pixels = np.asarray(pixels, dtype=np.uint16)
        if pixels.ndim != 2 or pixels.shape[1] != self.width:
            raise ValueError(f"Pixels must have shape (rows, {self.width})")
        if not isinstance(y, int) or not 0 <= y <= self.height:
            raise ValueError("Starting row must be within the display")
        with self._lock:
            self._require_open()
            rows = min(len(pixels), self.height - y)
            if rows <= 0:
                return
            self._write_rect(pixels[:rows], 0, y)
            # A public blit changes physical rows outside show's cached image.
            self._last = None

    def show(self, image: Image.Image) -> None:
        """Send only the rectangles that changed since the last frame."""
        if image.size != self.size:
            image = image.resize(self.size)
        pixels = to_rgb565(image)
        with self._lock:
            self._require_open()
            if self._last is None:
                self._write_rect(pixels, 0, 0)
            else:
                for x0, y0, x1, y1 in changed_rects(self._last, pixels):
                    self._write_rect(pixels[y0:y1, x0:x1], x0, y0)
            self._last = pixels

    def clear(self, color: str = "black") -> None:
        with self._lock:
            self._require_open()
            self._last = None
            self.show(Image.new("RGB", self.size, color))

    def read_touch(self):
        """Return raw (x, y, z1, z2), None on release, or TOUCH_DOWN_INVALID."""
        with self._lock:
            self._require_open()
            values = [ctypes.c_int() for _ in range(4)]
            ctypes.set_errno(0)
            result = self._native.kedei_read_touch(self._handle, *(ctypes.byref(v) for v in values))
            if result < 0:
                raise _native_error("Reading KeDei touch")
            if result == 0:
                return None
            if result == 2:
                return TOUCH_DOWN_INVALID
            if result != 1:
                raise RuntimeError(f"Unexpected KeDei touch status {result}")
            return tuple(value.value for value in values)

    def close(self) -> None:
        with self._lock:
            if self._closing or self._handle is None:
                return
            self._closing = True
            watchers = tuple(self._watchers)
        # Join outside the display lock so a read already waiting for it can exit.
        for watcher in watchers:
            watcher.stop()
        for watcher in watchers:
            if watcher is not threading.current_thread() and watcher.is_alive():
                watcher.join()
        with self._lock:
            handle, self._handle = self._handle, None
            ctypes.set_errno(0)
            if self._native.kedei_close(handle) < 0:
                raise _native_error("Closing the shared KeDei transport")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class XPT2046Watcher(threading.Thread):
    """Call on_tap(raw_x, raw_y, seconds_held, start, end) on a genuine physical release,
    like touch.TapWatcher: start and end are raw (x, y) medians of the first and last
    few samples, so the app can tell a swipe from a tap.

    Samples use the display's shared handle. Invalid ADC samples while PENIRQ
    stays low preserve the gesture, including holds of 0.8 seconds or longer.
    The median raw point is calculated from at most the latest 512 valid samples.
    """

    def __init__(self, display: KeDeiDisplay, on_tap, poll: float = 0.02,
                 cooldown: float = 0.25):
        super().__init__(daemon=True, name="kedei-touch")
        if not callable(on_tap):
            raise TypeError("on_tap must be callable")
        if not math.isfinite(poll) or poll <= 0 or not math.isfinite(cooldown) or cooldown < 0:
            raise ValueError("Touch poll must be positive and cooldown nonnegative")
        self.display = display
        self.on_tap = on_tap
        self.poll = poll
        self.cooldown = cooldown
        self.error: Exception | None = None
        self._stop_event = threading.Event()
        display._register_watcher(self)

    def stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        self.stop()
        if self is not threading.current_thread() and self.is_alive():
            self.join()
        if not self.is_alive():
            self.display._unregister_watcher(self)

    def run(self) -> None:
        pressed = None
        samples = deque(maxlen=512)
        first: list[tuple[int, int]] = []   # kept apart: the deque drops them on long holds
        last = float("-inf")
        try:
            while not self._stop_event.is_set():
                reading = self.display.read_touch()
                if self._stop_event.is_set():
                    break
                now = time.monotonic()
                if reading is None:
                    if pressed is not None and samples and now - last >= self.cooldown:
                        held = now - pressed
                        last = now
                        self.on_tap(*median_point(samples), held, median_point(first),
                                    median_point(list(samples)[-EDGE_SAMPLES:]))
                    pressed = None
                    samples.clear()
                    first.clear()
                else:
                    if pressed is None:
                        pressed = now
                    if reading is not TOUCH_DOWN_INVALID:
                        samples.append(reading[:2])
                        if len(first) < EDGE_SAMPLES:
                            first.append(reading[:2])
                self._stop_event.wait(self.poll)
        except Exception as exc:
            if not self._stop_event.is_set() and not self.display.closed:
                self.error = exc
                print(f"KeDei touch stopped: {exc}", file=sys.stderr, flush=True)
        finally:
            self.display._unregister_watcher(self)


def main() -> None:
    import argparse
    from PIL import ImageDraw

    parser = argparse.ArgumentParser(prog="wifi_meter.kedei", description="KeDei V6.2 colour test card")
    parser.add_argument("--speed", type=float, default=4, help="LCD SPI clock in MHz (default: 4)")
    parser.add_argument("--touch", type=float, nargs="?", const=90, default=0, metavar="SECONDS",
                        help="then print raw taps for 1..180 seconds (default when omitted: 90)")
    args = parser.parse_args()
    if not math.isfinite(args.speed) or not 0.01 <= args.speed <= 32:
        parser.error("--speed must be between 0.01 and 32 MHz")
    if args.touch and (not math.isfinite(args.touch) or not 1 <= args.touch <= 180):
        parser.error("--touch must be between 1 and 180 seconds")

    with KeDeiDisplay(speed_hz=int(args.speed * 1e6)) as lcd:
        image = Image.new("RGB", lcd.size, "black")
        draw = ImageDraw.Draw(image)
        colors = ["red", "#00ff00", "blue", "cyan", "magenta", "yellow", "white"]
        for i, color in enumerate(colors):
            draw.rectangle((20 + i * 440 // 7, 120, 20 + (i + 1) * 440 // 7 - 1, 220), fill=color)
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline="white", width=3)
        draw.text((24, 30), "KeDei v6.2 test card", fill="white")
        draw.text((24, 60), "top-left", fill="yellow")
        started = time.monotonic()
        lcd.show(image)
        print(f"full frame in {time.monotonic() - started:.2f}s at {args.speed:g} MHz", flush=True)
        if args.touch:
            watcher = XPT2046Watcher(
                lcd, lambda x, y, held, start, end: print(
                    f"tap x={x} y={y} held={held:.2f}s from {start} to {end}", flush=True)
            )
            watcher.start()
            try:
                deadline = time.monotonic() + args.touch
                print(f"Touch capture for {args.touch:g}s; press firmly, then release.", flush=True)
                while time.monotonic() < deadline and watcher.is_alive():
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                if watcher.error is not None:
                    raise watcher.error
            finally:
                watcher.close()


if __name__ == "__main__":
    main()
