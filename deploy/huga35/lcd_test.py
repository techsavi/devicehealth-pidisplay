#!/usr/bin/python3
"""Draw a test card on the HUGA 480x320 RGB565 framebuffer."""
import array
import mmap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
base = Path("/sys/class/graphics/fb0")
assert (base / "name").read_text().strip() == "fb_ili9486"
assert (base / "bits_per_pixel").read_text().strip() == "16"
w, h = map(int, (base / "virtual_size").read_text().strip().split(","))
stride = int((base / "stride").read_text())
im = Image.new("RGB", (w, h), "#101820")
d = ImageDraw.Draw(im)
fontpath = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
large = ImageFont.truetype(fontpath, 34)
small = ImageFont.truetype(fontpath, 20)
d.text((25, 24), "Hello from the Pi!", font=large, fill="white")
d.text((25, 80), "HUGA LCD • 480 × 320", font=small, fill="white")
d.text((25, 115), "ILI9486 • SPI 16 MHz", font=small, fill="#78dcca")
colors = ["red", "#00ff00", "blue", "cyan", "magenta", "yellow", "white"]
for n, color in enumerate(colors):
    x = 20 + n * (w - 40) // len(colors)
    end = 20 + (n + 1) * (w - 40) // len(colors)
    d.rectangle((x, 175, end - 1, 255), fill=color)
d.rectangle((0, 0, w - 1, h - 1), outline="white", width=3)
d.text((25, 278), "Display driver is running", font=small, fill="white")
with open("/dev/fb0", "r+b", buffering=0) as f:
    with mmap.mmap(f.fileno(), stride * h) as fb:
        pixels = list(im.getdata())
        for y in range(h):
            row = array.array("H", [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                                    for r, g, b in pixels[y * w:(y + 1) * w]])
            fb[y * stride:y * stride + w * 2] = row.tobytes()
print(f"Test card written to /dev/fb0: {w}x{h}, RGB565")
