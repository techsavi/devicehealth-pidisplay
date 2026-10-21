"""Write Pillow images to the HUGA 480x320 ILI9486 RGB565 framebuffer, or open the
KeDei V6.2 backend; both send only what changed since the last frame."""
import mmap
from pathlib import Path

import numpy as np
from PIL import Image


def to_rgb565(image: Image.Image) -> np.ndarray:
    """Convert an image to a (height, width) array of little-endian RGB565 pixels."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)).astype("<u2")


def changed_rects(old: np.ndarray, new: np.ndarray, gap: int = 8) -> list[tuple[int, int, int, int]]:
    """Rectangles (x0, y0, x1, y1), end-exclusive, covering every pixel that differs.

    Changed rows less than ``gap`` rows apart share one band, so a line of text is one
    rectangle rather than one per glyph row; each band is trimmed to its changed columns.
    """
    diff = old != new
    rows = np.flatnonzero(diff.any(axis=1))
    if not len(rows):
        return []
    breaks = np.flatnonzero(np.diff(rows) > gap)
    starts = np.concatenate(([rows[0]], rows[breaks + 1]))
    ends = np.concatenate((rows[breaks], [rows[-1]])) + 1
    rects = []
    for y0, y1 in zip(starts, ends):
        cols = np.flatnonzero(diff[y0:y1].any(axis=0))
        rects.append((int(cols[0]), int(y0), int(cols[-1]) + 1, int(y1)))
    return rects


class Framebuffer:
    def __init__(self, device: str = "/dev/fb0"):
        sysfs = Path("/sys/class/graphics") / Path(device).name
        bpp = int((sysfs / "bits_per_pixel").read_text())
        if bpp != 16:
            raise RuntimeError(f"{device} is {bpp} bpp, expected 16 (RGB565)")
        self.width, self.height = map(
            int, (sysfs / "virtual_size").read_text().strip().split(","))
        self.stride = int((sysfs / "stride").read_text())
        self._file = open(device, "r+b", buffering=0)
        self._mem = mmap.mmap(self._file.fileno(), self.stride * self.height)
        self._last: np.ndarray | None = None  # what show() last put on screen

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def blit(self, pixels: np.ndarray, y: int = 0) -> None:
        """Write full-width RGB565 rows (from to_rgb565) starting at screen row ``y``."""
        self._last = None  # the screen no longer matches show()'s copy
        self._write(pixels, y)

    def _write(self, pixels: np.ndarray, y: int) -> None:
        rows = min(len(pixels), self.height - y)
        if rows <= 0:
            return
        if self.stride == self.width * 2:
            start = y * self.stride
            self._mem[start:start + rows * self.stride] = pixels[:rows].tobytes()
            return
        row_bytes = self.width * 2
        for i in range(rows):
            offset = (y + i) * self.stride
            self._mem[offset:offset + row_bytes] = pixels[i].tobytes()

    def show(self, image: Image.Image) -> None:
        """Write only the rows that changed since the last frame: the SPI panel's driver
        sends every framebuffer page written to, changed or not."""
        if image.size != self.size:
            image = image.resize(self.size)
        pixels = to_rgb565(image)
        if self._last is None:
            self._write(pixels, 0)
        else:
            for _, y0, _, y1 in changed_rects(self._last, pixels):
                self._write(pixels[y0:y1], y0)
        self._last = pixels

    def clear(self, color: str = "black") -> None:
        self._last = None
        self.show(Image.new("RGB", self.size, color))

    def close(self) -> None:
        self._mem.close()
        self._file.close()


def open_display(device: str = "/dev/fb0", backend: str = "auto",
                 speed_hz: int = 4_000_000):
    """Select an existing framebuffer or the configured KeDei V6.2 SPI bus."""
    if backend == "framebuffer" or (backend == "auto" and Path(device).exists()):
        return Framebuffer(device)
    from .kedei import KeDeiDisplay, available
    if backend == "kedei" or (backend == "auto" and available()):
        return KeDeiDisplay(speed_hz=speed_hz)
    raise RuntimeError(
        f"No framebuffer at {device} or configured KeDei display. "
        "For KeDei V6.2, enable dtoverlay=spi0-1cs,cs0_pin=7 and use --backend kedei."
    )
