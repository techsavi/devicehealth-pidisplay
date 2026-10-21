"""The "Server Status" view: the webserver's Server status tile, from MQTT.

../statuspanel's poller publishes the tile's data (its /api/summary) as retained
devices/statuspanel/summary after every poll, and keeps status/statuspanel at "online"
(last will "offline"). This draws the same things the tile does: a healthy / warning /
critical / stale pill, host · updated N ago · site up/down, CPU, Memory, Disk /, Certs
and Updates boxes (amber from 80 %, red from 90 %), the top issues, and the poller's own
running state in place of the tile's supervisor row.
"""
import time

from PIL import Image, ImageDraw

from .render import (BG, CARD, DIM, HEADER_H, MARGIN, RULE, TEXT, _draw_alert, _draw_identify,
                     _fit, _right, _wrap, draw_header, font)

# The tile's colours (../webserver/front/public/index.html: --ok, --warn, --bad, --line, --muted).
OK, WARN, BAD, PLAIN_BG, PLAIN = "#22c55e", "#f59e0b", "#ef4444", "#2a3542", "#8b98a5"
PILLS = {"ok": ("healthy", OK), "warning": ("warning", WARN), "critical": ("critical", BAD)}
LEVEL_COLORS = {"warning": WARN, "critical": BAD}

DEMO = {
    "host": "web-01", "overall": "warning", "generated_at": time.time() - 200,
    "last_success": time.time() - 200, "stale": False, "error": None, "site_up": True,
    "alerts": {"critical": 1, "warning": 3, "info": 1, "top": [
        {"level": "critical", "area": "TLS", "message": "Certificate for shop.example.com expired 2 days ago",
         "since": time.time() - 7200},
        {"level": "warning", "area": "Disk", "message": "/var is 86% full", "since": time.time() - 90000},
        {"level": "warning", "area": "Updates", "message": "3 security updates are waiting to be installed",
         "since": time.time() - 4000},
        {"level": "warning", "area": "Logins", "message": "41 failed SSH logins in 24 h", "since": time.time() - 600},
    ]},
    "metrics": {"cpu_pct": 12.4, "mem_pct": 83.1, "disk_root_pct": 91.0},
    "tls": {"domains": 6, "invalid": 1, "min_days_left": 9.5},
    "updates": {"security": 3, "upgradable": 7, "reboot_required": False},
    "interval_seconds": 300, "poller_started": time.time() - 3 * 3600,
}


def ago(ts: float | None) -> str:
    if not ts:
        return "—"
    s = time.time() - ts
    return "just now" if s < 60 else f"{int(s / 60 + .5)} min ago" if s < 3600 else f"{int(s / 3600 + .5)} h ago"


def uptime(seconds: float) -> str:
    if seconds < 3600:
        return f"up {max(1, int(seconds / 60 + .5))} min"
    return f"up {int(seconds / 3600 + .5)} h" if seconds < 86400 else f"up {int(seconds / 86400 + .5)} d"


def _mix(color: str, amount: float) -> str:
    """``color`` blended over the background, like the tile's color-mix() pills."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(BG[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * amount + y * (1 - amount)):02x}" for x, y in zip(a, b))


def _pill(draw: ImageDraw.ImageDraw, right: int, text: str, color: str | None) -> None:
    pill_font = font(14, bold=True)
    w = draw.textlength(text, font=pill_font) + 20
    fill, fg = (_mix(color, 0.22), color) if color else (PLAIN_BG, PLAIN)
    draw.rounded_rectangle((right - w, 5, right, 27), radius=11, fill=fill)
    draw.text((right - w + 10, 7), text, font=pill_font, fill=fg)


def _pct(value) -> tuple[str, str]:
    if value is None:
        return "—", TEXT
    return f"{round(value)}%", BAD if value >= 90 else WARN if value >= 80 else TEXT


def _stats(s: dict) -> list[tuple[str, str, str]]:
    m, t, u = s.get("metrics") or {}, s.get("tls") or {}, s.get("updates") or {}
    if t.get("invalid"):
        certs = (f"{t['invalid']} bad", BAD)
    elif t.get("min_days_left") is None:
        certs = ("—", TEXT)
    else:
        certs = (f"{int(t['min_days_left'])} d", WARN if t["min_days_left"] < 14 else TEXT)
    if u.get("security"):
        updates = (f"{u['security']} sec", WARN)
    elif u.get("reboot_required"):
        updates = ("reboot", WARN)
    else:
        updates = (str(u["upgradable"]) if u.get("upgradable") is not None else "—", TEXT)
    return [("CPU", *_pct(m.get("cpu_pct"))), ("Memory", *_pct(m.get("mem_pct"))),
            ("Disk /", *_pct(m.get("disk_root_pct"))), ("Certs", *certs), ("Updates", *updates)]


def _spans(draw: ImageDraw.ImageDraw, x: int, y: int, spans, max_x: int) -> None:
    """Draw (text, color, bold) pieces on one line, truncating the last that doesn't fit."""
    for text, color, bold in spans:
        fnt = font(14, bold=bold)
        if x + draw.textlength(text, font=fnt) > max_x:
            draw.text((x, y), _fit(draw, text, fnt, int(max_x - x)), font=fnt, fill=color)
            return
        draw.text((x, y), text, font=fnt, fill=color)
        x += draw.textlength(text, font=fnt)


def _footer(draw: ImageDraw.ImageDraw, width: int, height: int, server: dict) -> None:
    y = height - 26
    draw.line((0, y - 6, width, y - 6), fill=RULE)
    presence, summary = server.get("presence"), server.get("summary") or {}
    if presence == "online":
        led, text = OK, "StatusPanel running"
        if summary.get("poller_started"):
            text += f" · {uptime(time.time() - summary['poller_started'])}"
    elif presence == "offline":
        led, text = BAD, "StatusPanel stopped"
    else:
        led, text = PLAIN_BG, "StatusPanel —"
    draw.ellipse((MARGIN + 2, y + 4, MARGIN + 12, y + 14), fill=led)
    draw.text((MARGIN + 20, y), text, font=font(13), fill=DIM)
    interval = summary.get("interval_seconds")
    if interval:
        _right(draw, width - MARGIN - 2, y, f"polls every {round(interval / 60)} min", font(13), DIM)


def _message(draw: ImageDraw.ImageDraw, width: int, height: int, text: str, color: str = DIM) -> None:
    msg_font = font(18)
    lines = _wrap(draw, text, msg_font, width - 60, 3)
    y = (HEADER_H + height) // 2 - 12 * len(lines)
    for line in lines:
        draw.text(((width - draw.textlength(line, font=msg_font)) / 2, y), line, font=msg_font, fill=color)
        y += 24


def render_server(server: dict, size=(480, 320), bus_connected: bool | None = None,
                  view: tuple[int, int] = (1, 2), alert=None, alert_index: tuple[int, int] = (1, 1),
                  identify: dict | None = None) -> Image.Image:
    """``server`` is {"summary": dict | None, "presence": "online" | "offline" | None}."""
    width, height = size
    image = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(image)
    if identify is not None:
        _draw_identify(draw, width, height, identify)
        return image

    s, presence = server.get("summary"), server.get("presence")
    draw_header(draw, width, "Server Status", "", DIM, bus_connected, view)
    if s is None:
        _pill(draw, width - MARGIN, "unavailable", BAD)
    elif presence == "offline":
        _pill(draw, width - MARGIN, "unavailable", BAD)
    else:
        interval = s.get("interval_seconds") or 300
        stale = bool(s.get("last_success")) and time.time() - s["last_success"] > interval * 3
        label, color = ("stale", None) if stale else PILLS.get(s.get("overall"), (s.get("overall") or "unknown", None))
        _pill(draw, width - MARGIN, label, color)

    if alert is not None:
        _draw_alert(draw, width, height, alert, *alert_index)
        return image
    _footer(draw, width, height, server)

    if s is None:
        if bus_connected is False:
            _message(draw, width, height - 30, "Not connected to the MQTT broker")
        elif presence == "offline":
            _message(draw, width, height - 30, "StatusPanel is stopped and nothing has been published yet")
        else:
            _message(draw, width, height - 30, "Waiting for StatusPanel…")
        return image
    if not s.get("generated_at"):
        _message(draw, width, height - 30, s.get("error") or "No data collected yet.")
        return image

    # host · updated N ago · site up / site down · last poll failed
    y = HEADER_H + 8
    spans = [(str(s.get("host") or "?"), TEXT, True), (f" · updated {ago(s.get('last_success'))}", DIM, False)]
    if s.get("site_up") is False:
        spans += [(" · ", DIM, False), ("site down", BAD, True)]
    elif s.get("site_up"):
        spans.append((" · site up", DIM, False))
    _spans(draw, MARGIN + 2, y, spans, width - MARGIN)
    if s.get("error"):
        y += 20
        _spans(draw, MARGIN + 2, y, [(f"last poll failed: {s['error']}", WARN, False)], width - MARGIN)

    # CPU · Memory · Disk / · Certs · Updates
    y += 26
    gap, boxes = 6, _stats(s)
    box_w = (width - 2 * MARGIN - gap * (len(boxes) - 1)) / len(boxes)
    for i, (label, value, color) in enumerate(boxes):
        x0 = round(MARGIN + i * (box_w + gap))
        x1 = round(MARGIN + i * (box_w + gap) + box_w) - 1
        draw.rounded_rectangle((x0, y, x1, y + 58), radius=8, fill=CARD, outline=RULE)
        draw.text((x0 + 9, y + 6), label, font=font(12), fill=DIM)
        value_font = font(22, bold=True)
        draw.text((x0 + 9, y + 24), _fit(draw, value, value_font, x1 - x0 - 14), font=value_font, fill=color)
    y += 70

    # The top issues (the tile shows three), then "+N more".
    alerts = s.get("alerts") or {}
    top = alerts.get("top") or []
    footer_top = height - 34
    if not top:
        draw.text((MARGIN + 2, y + 4), "✓ No warnings - all checks passing.", font=font(16), fill=OK)
        return image
    issue_font, shown = font(15), 0
    for issue in top[:3]:
        age = ago(issue.get("since")) if issue.get("since") else ""
        age_w = draw.textlength(age, font=font(12)) + 10 if age else 0
        lines = _wrap(draw, str(issue.get("message") or ""), issue_font, int(width - 2 * MARGIN - 22 - age_w), 2)
        block = 20 * len(lines) + 8
        if y + block > footer_top - 18:
            break
        draw.ellipse((MARGIN + 3, y + 5, MARGIN + 13, y + 15), fill=LEVEL_COLORS.get(issue.get("level"), PLAIN))
        for i, line in enumerate(lines):
            draw.text((MARGIN + 22, y + i * 20), line, font=issue_font, fill=TEXT)
        if age:
            _right(draw, width - MARGIN - 2, y + 2, age, font(12), DIM)
        y += block
        shown += 1
    more = (alerts.get("critical") or 0) + (alerts.get("warning") or 0) - shown
    if more > 0:
        draw.text((MARGIN + 22, y), f"+{more} more", font=font(13), fill=DIM)
    return image
