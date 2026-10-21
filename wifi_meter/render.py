"""Draw the Wi-Fi signal meter screen."""
import time
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .scanner import Network

BG = "#101820"
STRIPE = "#141f29"
TEXT = "white"
DIM = "#7d8d9c"
ACCENT = "#78dcca"
WARN = "#f5a524"
RULE = "#2a3a48"
SEG_OFF = "#1f2c38"
SEG_LOW = "#e5484d"
SEG_MID = "#f5a524"
SEG_HIGH = "#30c46c"
DISABLED = "#33434f"
CARD = "#18232e"
ALERT_COLORS = {"info": "#4ea1ff", "warning": WARN, "critical": SEG_LOW}

WIDTH, HEIGHT = 480, 320
# Scan time in the Wi-Fi header. Minutes, not seconds, so a scan with the same results
# doesn't change the screen and nothing is redrawn.
STAMP_FORMAT = "%H:%M"
HEADER_H = 32
ROW_TOP = 36
ROW_H = 28
MARGIN = 8

# Right-hand scroll strip: ▲ button, track with thumb, ▼ button.
STRIP_X = 436
BUTTON_H = 44
TRACK_W = 6

# Columns of a row: name | bar | channel | percent
NAME_RIGHT = 192
BAR_LEFT, BAR_RIGHT = 198, 308
CHAN_RIGHT = 362
PCT_RIGHT = STRIP_X - 10
SEGMENTS = 10
SEG_GAP = 2

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "C:/Windows/Fonts/"]
FONT_FILES = {False: ["DejaVuSans.ttf", "arial.ttf"],
              True: ["DejaVuSans-Bold.ttf", "arialbd.ttf"]}


def visible_rows(height: int = HEIGHT) -> int:
    return (height - ROW_TOP) // ROW_H


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for directory in FONT_DIRS:
        for name in FONT_FILES[bold]:
            try:
                return ImageFont.truetype(directory + name, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _fit(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> str:
    if draw.textlength(text, font=fnt) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_width:
        text = text[:-1]
    return text + "…"


def _right(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fnt, fill) -> None:
    draw.text((x - draw.textlength(text, font=fnt), y), text, font=fnt, fill=fill)


def _signal_color(signal: int) -> str:
    if signal < 30:
        return SEG_LOW
    if signal < 55:
        return SEG_MID
    return SEG_HIGH


def _draw_bar(draw: ImageDraw.ImageDraw, y0: int, y1: int, signal: int):
    lit = round(signal * SEGMENTS / 100)
    color = _signal_color(signal)
    seg_w = (BAR_RIGHT - BAR_LEFT - SEG_GAP * (SEGMENTS - 1)) / SEGMENTS
    for i in range(SEGMENTS):
        left = round(BAR_LEFT + i * (seg_w + SEG_GAP))
        right = round(BAR_LEFT + i * (seg_w + SEG_GAP) + seg_w) - 1
        draw.rectangle((left, y0, right, y1), fill=color if i < lit else SEG_OFF)


def _draw_row(draw: ImageDraw.ImageDraw, y: int, net: Network, stripe: bool):
    if stripe:
        draw.rectangle((0, y, STRIP_X - 1, y + ROW_H - 1), fill=STRIPE)
    text_y = y + (ROW_H - 18) // 2

    name = net.ssid or "(hidden)"
    if net.in_use:
        name = "● " + name
    name_font = font(15, bold=net.in_use)
    color = ACCENT if net.in_use else (DIM if net.hidden else TEXT)
    draw.text((MARGIN, text_y), _fit(draw, name, name_font, NAME_RIGHT - MARGIN),
              font=name_font, fill=color)

    _draw_bar(draw, y + 8, y + ROW_H - 9, net.signal)
    _right(draw, CHAN_RIGHT, text_y + 2, f"ch {net.channel}", font(13), DIM)
    _right(draw, PCT_RIGHT, text_y, f"{net.signal}%", font(15, bold=True), TEXT)


def _arrow(draw: ImageDraw.ImageDraw, cx: int, cy: int, up: bool, enabled: bool):
    dy = -9 if up else 9
    draw.polygon([(cx, cy + dy), (cx - 11, cy - dy), (cx + 11, cy - dy)],
                 fill=TEXT if enabled else DISABLED)


def _draw_scrollbar(draw: ImageDraw.ImageDraw, width: int, height: int,
                    first: int, shown: int, total: int):
    draw.line((STRIP_X, ROW_TOP, STRIP_X, height), fill=RULE)
    cx = (STRIP_X + width) // 2
    _arrow(draw, cx, ROW_TOP + BUTTON_H // 2, up=True, enabled=first > 0)
    _arrow(draw, cx, height - BUTTON_H // 2, up=False, enabled=first + shown < total)

    top, bottom = ROW_TOP + BUTTON_H, height - BUTTON_H
    x0, x1 = cx - TRACK_W // 2, cx + TRACK_W // 2 - 1
    draw.rounded_rectangle((x0, top, x1, bottom), radius=3, fill=SEG_OFF)
    if total > shown:
        track = bottom - top
        thumb = max(16, round(track * shown / total))
        y = top + round((track - thumb) * first / (total - shown))
        draw.rounded_rectangle((x0, y, x1, y + thumb), radius=3, fill=DIM)


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=fnt) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = word if draw.textlength(word, font=fnt) <= max_width else _fit(draw, word, fnt, max_width)
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(draw, lines[-1] + " …", fnt, max_width)
    return lines


def _draw_alert(draw: ImageDraw.ImageDraw, width: int, height: int, alert, index: int, count: int):
    color = ALERT_COLORS.get(alert.level, ACCENT)
    x0, y0, x1, y1 = 10, ROW_TOP + 8, width - 10, height - 10
    draw.rounded_rectangle((x0, y0, x1, y1), radius=10, fill=CARD, outline=color, width=3)
    inner_l, inner_r = x0 + 14, x1 - 14

    draw.text((inner_l, y0 + 12), alert.level.upper(), font=font(14, bold=True), fill=color)
    stamp = time.strftime("%H:%M", time.localtime(alert.since))
    _right(draw, inner_r, y0 + 13, f"{alert.source} · {stamp}", font(13), DIM)

    y = y0 + 38
    title_font = font(22, bold=True)
    for line in _wrap(draw, alert.title, title_font, inner_r - inner_l, 2) if alert.title else []:
        draw.text((inner_l, y), line, font=title_font, fill=TEXT)
        y += 28
    footer_y = y1 - 26
    msg_font = font(17)
    max_lines = max(0, (footer_y - 6 - y) // 22)
    for line in _wrap(draw, alert.message, msg_font, inner_r - inner_l, max_lines) if alert.message else []:
        draw.text((inner_l, y + 4), line, font=msg_font, fill=TEXT)
        y += 22

    draw.text((inner_l, footer_y), "tap to dismiss", font=font(13), fill=DIM)
    if count > 1:
        _right(draw, inner_r, footer_y, f"{index} of {count}", font(13), DIM)


IDENTIFY_BG = "#ffd23f"   # loud and unlike any normal screen, so the Pi stands out among several
IDENTIFY_TEXT = "#101820"


def _draw_identify(draw: ImageDraw.ImageDraw, width: int, height: int, identify: dict):
    """The whole screen: this Pi's name, as large as fits, its subtitle and a countdown."""
    draw.rectangle((0, 0, width, height), fill=IDENTIFY_BG)
    draw.rectangle((6, 6, width - 7, height - 7), outline=IDENTIFY_TEXT, width=4)
    draw.text((22, 18), "IDENTIFY", font=font(16, bold=True), fill=IDENTIFY_TEXT)

    name = identify.get("text") or "?"
    size = 96
    while size > 24 and draw.textlength(name, font=font(size, bold=True)) > width - 48:
        size -= 4
    name_font = font(size, bold=True)
    subtitle = identify.get("subtitle") or ""
    sub_size = max(16, min(26, round(size * 0.6)))   # never larger than the name it explains
    sub_font = font(sub_size)
    block = size + (sub_size + 14 if subtitle else 0)
    y = (height - block) // 2 - 8
    draw.text(((width - draw.textlength(_fit(draw, name, name_font, width - 48), font=name_font)) / 2, y),
              _fit(draw, name, name_font, width - 48), font=name_font, fill=IDENTIFY_TEXT)
    if subtitle:
        sub = _fit(draw, subtitle, sub_font, width - 48)
        draw.text(((width - draw.textlength(sub, font=sub_font)) / 2, y + size + 12), sub, font=sub_font, fill=IDENTIFY_TEXT)

    left = max(0, round(identify["until"] - time.time()))
    footer = f"tap to dismiss · {left} s"
    foot_font = font(15)
    draw.text(((width - draw.textlength(footer, font=foot_font)) / 2, height - 36), footer, font=foot_font, fill=IDENTIFY_TEXT)


def draw_header(draw: ImageDraw.ImageDraw, width: int, title: str, right: str, right_color: str,
                bus_connected: bool | None = None, view: tuple[int, int] = (0, 1)) -> None:
    """Title, MQTT dot (green = connected; None hides it), one dot per view with the
    current one filled (swipe left/right to switch), and a status line on the right."""
    title_font = font(20, bold=True)
    draw.text((MARGIN + 2, 5), title, font=title_font, fill=TEXT)
    x = MARGIN + 2 + draw.textlength(title, font=title_font) + 12
    if bus_connected is not None:
        draw.ellipse((x - 4, 12, x + 4, 20), fill=SEG_HIGH if bus_connected else DISABLED)
        x += 22
    current, count = view
    if count > 1:
        for i in range(count):
            cx = x + i * 13
            if i == current:
                draw.ellipse((cx - 4, 12, cx + 4, 20), fill=TEXT)
            else:
                draw.ellipse((cx - 3, 13, cx + 3, 19), outline=DIM, width=1)
        x += count * 13
    right_font = font(14)
    _right(draw, width - MARGIN - 2, 10, _fit(draw, right, right_font, int(width - MARGIN - 8 - x)),
           right_font, right_color)
    draw.line((0, HEADER_H, width, HEADER_H), fill=RULE, width=1)


def render(networks: list[Network] | None, first: int = 0, size=(WIDTH, HEIGHT),
           status: str = "", updated: float | None = None, alert=None,
           alert_index: tuple[int, int] = (1, 1), bus_connected: bool | None = None,
           identify: dict | None = None, view: tuple[int, int] = (0, 1)) -> Image.Image:
    """Render the screen with ``networks[first:]`` at the top of the list.

    ``networks`` is None before the first scan completes. ``alert`` (a bus.Alert)
    covers the list with a card. ``bus_connected`` draws the MQTT status dot after
    the title (None hides it). ``identify`` ({text, subtitle, until}) covers the
    whole screen with the Pi's name, over everything else. ``view`` is (index, count)
    for the view dots in the header.
    """
    width, height = size
    image = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(image)
    if identify is not None:
        _draw_identify(draw, width, height, identify)
        return image

    if status:
        right, right_color = status, WARN
    elif networks is None:
        right, right_color = "scanning…", DIM
    else:
        stamp = time.strftime(STAMP_FORMAT, time.localtime(updated or time.time()))
        right, right_color = f"{len(networks)} networks · {stamp}", DIM
    draw_header(draw, width, "Wi-Fi Signal", right, right_color, bus_connected, view)

    if alert is not None:
        _draw_alert(draw, width, height, alert, *alert_index)
        return image

    rows = visible_rows(height)
    total = len(networks or ())
    _draw_scrollbar(draw, width, height, first, min(rows, total), total)

    if not networks:
        message = "Scanning for networks…" if networks is None else "No networks found"
        msg_font = font(20)
        draw.text(((STRIP_X - draw.textlength(message, font=msg_font)) / 2,
                   (ROW_TOP + height) / 2 - 12), message, font=msg_font, fill=DIM)
        return image

    for i, net in enumerate(networks[first:first + rows]):
        _draw_row(draw, ROW_TOP + i * ROW_H, net, stripe=(first + i) % 2 == 1)
    return image
