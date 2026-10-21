"""Touchscreen calibration: tap each target, then check taps land where you touch.

    sudo systemctl stop wifi-meter
    python3 -m wifi_meter.calibrate
    sudo systemctl start wifi-meter
"""
import argparse
import queue

from PIL import Image, ImageDraw

from . import touch
from .display import open_display
from .render import ACCENT, BG, DIM, TEXT, font

INSET = 40


def _screen(size, message: str, target=None, dots=()) -> Image.Image:
    image = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(image)
    msg_font = font(18)
    draw.text(((size[0] - draw.textlength(message, font=msg_font)) / 2, size[1] / 2 - 10),
              message, font=msg_font, fill=TEXT)
    if target:
        x, y = target
        draw.line((x - 18, y, x + 18, y), fill=ACCENT, width=2)
        draw.line((x, y - 18, x, y + 18), fill=ACCENT, width=2)
        draw.ellipse((x - 8, y - 8, x + 8, y + 8), outline=ACCENT, width=2)
    for x, y in dots:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=DIM)
    return image


def main() -> None:
    parser = argparse.ArgumentParser(prog="wifi_meter.calibrate", description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="/dev/fb0")
    parser.add_argument("--backend", choices=("auto", "framebuffer", "kedei"), default="auto")
    parser.add_argument("--spi-speed", type=float, default=4, help="KeDei SPI clock in MHz")
    parser.add_argument("--touch-device")
    parser.add_argument("--verify", type=float, default=20, help="seconds of tap testing afterwards")
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for each target")
    args = parser.parse_args()

    fb = open_display(args.device, args.backend, int(args.spi_speed * 1e6))
    w, h = fb.size
    taps: queue.Queue = queue.Queue()
    watcher = touch.start_watcher(fb, lambda x, y, *_: taps.put((x, y)), args.touch_device)
    if not watcher:
        fb.close()
        raise SystemExit("touchscreen not found")

    targets = [(INSET, INSET), (w - INSET, INSET), (w - INSET, h - INSET), (INSET, h - INSET)]
    points = []
    try:
        for n, target in enumerate(targets, 1):
            fb.show(_screen(fb.size, f"Tap the target ({n}/{len(targets)})", target))
            raw = taps.get(timeout=args.timeout)
            print(f"target {target} -> raw {raw}", flush=True)
            points.append((target, raw))

        cal = touch.Calibration.fit(points)
        cal.save()
        print(f"saved {touch.CALIBRATION_FILE}: {cal}", flush=True)
        for target, raw in points:
            print(f"  check {target} -> {cal.to_screen(*raw, fb.size)}")

        dots = []
        fb.show(_screen(fb.size, "Calibrated - tap anywhere to test"))
        while True:
            raw = taps.get(timeout=args.verify)
            dots.append(cal.to_screen(*raw, fb.size))
            print(f"tap raw {raw} -> screen {dots[-1]}", flush=True)
            fb.show(_screen(fb.size, "Calibrated - tap anywhere to test", dots=dots))
    except queue.Empty:
        if len(points) < len(targets):
            raise SystemExit("timed out waiting for a tap; calibration not saved")
    finally:
        if hasattr(watcher, "stop"):
            watcher.stop()
            watcher.join()
        try:
            fb.clear()
        finally:
            fb.close()


if __name__ == "__main__":
    main()
