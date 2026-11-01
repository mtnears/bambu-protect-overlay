"""
Graphical HUD renderer for bambu-protect-overlay ("overlay_style: hud").

render(view, size) returns a mostly transparent RGBA image the size of the video
frame. The overlay service writes it as <name>_hud.png once a second and go2rtc's
ffmpeg composites it onto the stream (overlay_vaapi on Intel, or plain overlay on
CPU), replacing the drawbox/drawtext text overlay.

`view` is a plain dict built by bambu_overlay.build_view(); this module knows
nothing about MQTT.

Layout (1680x1080 shown; positions scale with the frame width):
  top banner    printer name + status pill (left)  |  job title + profile (right)
  lower panel   full-width progress bar with pause markers
                left column (fixed slots): date/time, filament, pause alert
                instrument row: layer, timer, nozzle, bed, chamber, speed,
                AMS dryness, logo badge
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Fonts and colours
# ---------------------------------------------------------------------------

_FONT_DIRS = ["/usr/share/fonts/opentype/inter", "/usr/share/fonts/truetype/inter"]
_FALLBACK = {
    "Medium": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "SemiBold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "Bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "ExtraBold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}


@lru_cache(maxsize=None)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for ext in ("otf", "ttf"):
            p = os.path.join(d, f"Inter-{weight}.{ext}")
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.truetype(_FALLBACK[weight], size)
    except OSError:
        return ImageFont.load_default(size)


STATUS_COLORS = {
    "Printing": (64, 200, 120), "Preparing": (90, 170, 255), "Paused": (255, 176, 32),
    "Finished": (90, 170, 255), "Failed": (240, 70, 70), "Idle": (150, 150, 160),
}
WHITE = (245, 246, 248)
DIM = (175, 180, 190)
FAINT = (120, 125, 135)
TRACK = (58, 62, 70)
PANEL = (12, 14, 18)
AMBER = (255, 176, 32)
SPEED_MAX = 166          # Ludicrous


def humidity_color(n):
    return {1: (240, 70, 70), 2: (255, 140, 50), 3: (255, 200, 60),
            4: (140, 210, 90), 5: (64, 200, 120)}.get(n, FAINT)


def fmt_dur(minutes) -> str:
    if minutes is None or minutes < 0:
        return "-"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# Cached static layers
# ---------------------------------------------------------------------------

BANNER_H = 118
PANEL_H = 214


@lru_cache(maxsize=8)
def _banner(width: int) -> Image.Image:
    g = Image.new("RGBA", (width, BANNER_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    for y in range(BANNER_H):
        d.line((0, y, width, y), fill=PANEL + (int(230 * (1 - y / BANNER_H) ** 1.1),))
    return g


@lru_cache(maxsize=8)
def _panel(width: int) -> Image.Image:
    return Image.new("RGBA", (width, PANEL_H), PANEL + (205,))


@lru_cache(maxsize=8)
def _logo(path: str, mtime: float, height: int) -> Image.Image | None:
    try:
        im = Image.open(path).convert("RGBA")
    except OSError:
        return None
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] == 255:
        # Opaque artwork (e.g. black on white): turn dark ink into white on transparent.
        ink = im.convert("L").point(lambda v: 255 - v)
        im = Image.new("RGBA", im.size, WHITE + (0,))
        im.putalpha(ink)
        alpha = ink
    bbox = alpha.getbbox()
    if bbox:
        im = im.crop(bbox)
    return im.resize((max(1, round(im.width * height / im.height)), height), Image.LANCZOS)


def logo(path: str | None, height: int) -> Image.Image | None:
    if not path or not os.path.exists(path):
        return None
    return _logo(path, os.path.getmtime(path), height)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def shadow_text(img, xy, text, f, fill, anchor):
    """Text with a soft drop shadow, for legibility over the bright camera image.
    The blur is done on a small tile around the text, not the whole frame."""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = d.textbbox(xy, text, font=f, anchor=anchor)
    pad = 8
    box = (int(x0) - pad, int(y0) - pad, int(x1) + pad + 2, int(y1) + pad + 2)
    tile = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((xy[0] - box[0] + 1, xy[1] - box[1] + 2), text,
                              font=f, fill=(0, 0, 0, 220), anchor=anchor)
    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(2)), (box[0], box[1]))
    d.text(xy, text, font=f, fill=fill, anchor=anchor)


def fit_text(d, text, f, max_w):
    if d.textlength(text, font=f) <= max_w:
        return text
    while text and d.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def pill(d, x, y, text, color, f, h=34, pad=12):
    w = d.textlength(text, font=f) + 2 * pad + 22
    mix = tuple(int(c * 0.30 + p * 0.70) for c, p in zip(color, PANEL))
    d.rounded_rectangle((x, y, x + w, y + h), h // 2, fill=mix + (230,))
    d.ellipse((x + pad, y + h / 2 - 6, x + pad + 12, y + h / 2 + 6), fill=color + (255,))
    d.text((x + pad + 22, y + h / 2), text, font=f, fill=WHITE, anchor="lm")


def pause_icon(d, x, cy, h, color):
    w = h * 0.28
    d.rectangle((x, cy - h / 2, x + w, cy + h / 2), fill=color)
    d.rectangle((x + w * 1.9, cy - h / 2, x + w * 2.9, cy + h / 2), fill=color)


def dial(d, cx, cy, r, frac, center, sub, label, color, segments=0, tick=None):
    """240-degree instrument: track, value arc (or N segments), optional target tick."""
    start, sweep = 150, 240
    box = (cx - r, cy - r, cx + r, cy + r)
    if segments:
        gap = 6
        seg = (sweep - gap * (segments - 1)) / segments
        lit = round((frac or 0) * segments)
        for i in range(segments):
            a0 = start + i * (seg + gap)
            d.arc(box, a0, a0 + seg, fill=(color if i < lit else TRACK) + (255,), width=10)
    else:
        d.arc(box, start, start + sweep, fill=TRACK + (255,), width=10)
        if frac:
            d.arc(box, start, start + sweep * max(0.0, min(1.0, frac)), fill=color + (255,), width=10)
    if tick is not None:
        a = math.radians(start + sweep * max(0.0, min(1.0, tick)))
        d.line((cx + (r - 16) * math.cos(a), cy + (r - 16) * math.sin(a),
                cx + (r + 8) * math.cos(a), cy + (r + 8) * math.sin(a)), fill=WHITE + (255,), width=4)
    size = 34 if len(center) <= 3 else 30 if len(center) == 4 else 26
    d.text((cx, cy - 4), center, font=font("Bold", size), fill=WHITE, anchor="mm")
    d.text((cx, cy + 26), sub, font=font("Medium", 15), fill=DIM, anchor="mm")
    d.text((cx, cy + r - 2), label, font=font("SemiBold", 15), fill=DIM, anchor="mm")


def clock_dial(d, cx, cy, r, frac, center, sub, label, color, label_y):
    """Full-circle timer: one turn is the whole print; elapsed sweeps clockwise from 12."""
    box = (cx - r, cy - r, cx + r, cy + r)
    d.ellipse(box, outline=TRACK + (255,), width=10)
    f = max(0.0, min(1.0, frac or 0.0))
    if f:
        d.arc(box, -90, -90 + 360 * f, fill=color + (255,), width=10)
    for i in range(12):
        if i == 6:
            continue                      # leave room for the label
        a = math.radians(-90 + i * 30)
        r1, r2 = r + 7, r + (12 if i % 3 == 0 else 10)
        d.line((cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a)),
               fill=FAINT + (255,), width=2)
    a = math.radians(-90 + 360 * f)
    px, py = cx + r * math.cos(a), cy + r * math.sin(a)
    d.ellipse((px - 7, py - 7, px + 7, py + 7), fill=WHITE + (255,))
    d.text((cx, cy - 6), center, font=font("Bold", 22), fill=WHITE, anchor="mm")
    d.text((cx, cy + 22), sub, font=font("Medium", 15), fill=DIM, anchor="mm")
    d.text((cx, label_y), label, font=font("SemiBold", 15), fill=DIM, anchor="mm")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render(v: dict, size=(1680, 1080), now: datetime | None = None) -> Image.Image:
    """
    v keys (all optional unless noted):
      display (str, required), status (str, required), stage, job, profile,
      percent, layer, total_layers, remaining (min), elapsed_frac (0-1),
      nozzle, nozzle_t, bed, bed_t, chamber, chamber_t,
      filament, filament_color (r,g,b), speed_mag, speed_level,
      humidity_n (1-5), humidity_label,
      pauses: [{layer, remaining_at (min), note}], logo (path), site_label
    """
    W, H = size
    now = now or datetime.now()
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    status = v.get("status") or "-"
    sc = STATUS_COLORS.get(status, DIM)

    # ---------------- top banner ----------------
    img.alpha_composite(_banner(W), (0, 0))
    d = ImageDraw.Draw(img)
    x = 28
    if not v.get("logo") and v.get("site_label"):
        shadow_text(img, (x, 40), v["site_label"], font("Bold", 26), DIM, "lm")
        x += d.textlength(v["site_label"], font=font("Bold", 26)) + 22
    name = (v.get("display") or "").upper()
    shadow_text(img, (x, 40), name, font("ExtraBold", 38), WHITE, "lm")
    stage = v.get("stage") or ""
    plain = ("", "-", "printing", "idle", status.lower())
    pill_text = status.upper() if stage.lower() in plain else f"{status.upper()} · {stage}"
    pill(d, x, 66, pill_text, sc, font("Bold", 18))
    left_edge = x + max(d.textlength(name, font=font("ExtraBold", 38)),
                        d.textlength(pill_text, font=font("Bold", 18)) + 46) + 40

    idle = status in ("Idle", "-")
    if not idle and v.get("job"):
        f = font("SemiBold", 34)
        shadow_text(img, (W - 28, 40), fit_text(d, v["job"], f, W - 28 - left_edge), f, WHITE, "rm")
        if v.get("profile"):
            f2 = font("Medium", 19)
            shadow_text(img, (W - 28, 80), fit_text(d, v["profile"], f2, W - 28 - left_edge), f2,
                        (205, 210, 218), "rm")
    if idle:
        shadow_text(img, (W - 28, 40), now.strftime("%H:%M"), font("SemiBold", 30), WHITE, "rm")
        shadow_text(img, (W - 28, 74), now.strftime("%a %b %d %Y"), font("Medium", 17), (205, 210, 218), "rm")
        return img                              # idle: banner only, camera view stays clear

    # ---------------- lower panel ----------------
    py = H - PANEL_H
    img.alpha_composite(_panel(W), (0, py))
    d = ImageDraw.Draw(img)

    pct = int(v.get("percent") or 0)
    tl = int(v.get("total_layers") or 0)
    layer = int(v.get("layer") or 0)
    d.rectangle((0, py, W, py + 12), fill=(52, 56, 64, 255))
    d.rectangle((0, py, W * max(0, min(100, pct)) / 100, py + 12), fill=sc + (255,))
    pauses = v.get("pauses") or []
    for p in pauses:
        if not tl:
            break
        mx = W * p["layer"] / tl
        col = AMBER if p["layer"] > layer else (150, 120, 60)
        d.polygon([(mx - 10, py - 14), (mx + 10, py - 14), (mx, py)], fill=col + (255,))
        d.rectangle((mx - 2, py, mx + 2, py + 12), fill=col + (255,))

    # left column, fixed slots: date/time, filament, pause alert (empty if none)
    colw = 430
    top = py + 28
    d.text((28, top + 10), now.strftime("%a %b %d   %H:%M:%S"), font=font("SemiBold", 20), fill=DIM, anchor="lm")
    fy = top + 40
    d.text((28, fy), "FILAMENT", font=font("Medium", 15), fill=FAINT, anchor="lt")
    if v.get("filament_color"):
        d.rounded_rectangle((28, fy + 20, 52, fy + 44), 6, fill=tuple(v["filament_color"]) + (255,),
                            outline=(255, 255, 255, 255), width=2)
        fx = 62
    else:
        fx = 28
    d.text((fx, fy + 44), fit_text(d, v.get("filament") or "-", font("Bold", 28), colw - fx + 28),
           font=font("Bold", 28), fill=WHITE, anchor="ls")
    nxt = next((p for p in pauses if p["layer"] > layer), None)
    if nxt:
        ay, ah = top + 104, 58
        d.rounded_rectangle((28, ay, 28 + colw, ay + ah), 10, fill=AMBER + (255,))
        rem = v.get("remaining")
        mins = (rem - nxt["remaining_at"]) if (rem is not None and nxt.get("remaining_at") is not None) else None
        when = f"  ·  in {fmt_dur(mins)}  (~{(now + timedelta(minutes=mins)).strftime('%H:%M')})" if mins is not None and mins >= 0 else ""
        l1y = ay + (18 if nxt.get("note") else ah / 2)
        pause_icon(d, 44, l1y, 18, PANEL)
        d.text((70, l1y), f"PAUSE  layer {nxt['layer']}{when}", font=font("Bold", 19), fill=PANEL, anchor="lm")
        if nxt.get("note"):
            d.text((70, ay + 42), fit_text(d, nxt["note"], font("SemiBold", 18), colw - 52),
                   font=font("SemiBold", 18), fill=(70, 45, 0), anchor="lm")

    # instrument row, right-aligned
    gy = py + 104
    slot = 150
    x0 = W - 8 * slot + slot / 2 - 8

    def cx(i):
        return x0 + i * slot

    dial(d, cx(0), gy, 62, (layer / tl) if tl else pct / 100, f"{pct}%",
         f"{layer} / {tl}" if tl else "-", "LAYER", sc)
    rem = v.get("remaining")
    ef = v.get("elapsed_frac")
    clock_dial(d, cx(1), gy - 4, 56, ef if ef is not None else pct / 100, fmt_dur(rem),
               ("done " + (now + timedelta(minutes=rem)).strftime("%H:%M")) if rem else "-",
               "REMAINING", sc, gy + 60)
    for i, (lab, val, tgt, vmax, col) in enumerate([
        ("NOZZLE", v.get("nozzle"), v.get("nozzle_t"), 320, (255, 120, 70)),
        ("BED", v.get("bed"), v.get("bed_t"), 120, (255, 170, 60)),
        ("CHAMBER", v.get("chamber"), v.get("chamber_t"), 65, (90, 170, 255)),
    ]):
        on = bool(tgt and tgt > 0)
        dial(d, cx(2 + i), gy, 62, ((val or 0) / vmax) if val is not None else 0,
             "-" if val is None else f"{val:.0f}",
             f"→ {tgt:.0f}°" if on else ("off" if val is not None else ""), lab,
             col if on else FAINT, tick=(tgt / vmax) if on else None)
    mag = v.get("speed_mag")
    dial(d, cx(5), gy, 62, (mag or 0) / SPEED_MAX, "-" if mag is None else f"{mag}%",
         v.get("speed_level") or "", "SPEED", (180, 140, 255))
    hn = v.get("humidity_n")
    dial(d, cx(6), gy, 62, (hn or 0) / 5, "-" if hn is None else str(hn), v.get("humidity_label") or "",
         "AMS DRYNESS", humidity_color(hn), segments=5)
    lg = logo(v.get("logo"), 150)
    if lg is not None:
        # fit inside the last gauge slot: at most 150 px tall and (slot - 12) px wide
        box_w = slot - 12
        if lg.width > box_w:
            lg = lg.resize((box_w, max(1, round(lg.height * box_w / lg.width))), Image.LANCZOS)
        img.alpha_composite(lg, (int(cx(7) - lg.width / 2 + 4), int(gy + 2 - lg.height / 2)))
    elif v.get("site_label"):
        d.text((cx(7), gy), fit_text(d, v["site_label"], font("Bold", 18), slot - 10),
               font=font("Bold", 18), fill=DIM, anchor="mm")
    return img


def paused_banner(img: Image.Image, layer, note: str | None) -> None:
    """Large amber banner while the printer sits in a slicer pause."""
    W = img.width
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((W / 2 - 430, 150, W / 2 + 430, 260 if note else 230), 18, fill=AMBER + (235,))
    t = f"PAUSED AT LAYER {layer}  —  WAITING FOR YOU"
    f = font("ExtraBold", 30)
    tw = d.textlength(t, font=f)
    cy = 190
    pause_icon(d, W / 2 - tw / 2 - 22, cy, 28, PANEL)
    d.text((W / 2 + 14, cy), t, font=f, fill=PANEL, anchor="mm")
    if note:
        d.text((W / 2, 232), fit_text(d, note, font("SemiBold", 22), 820), font=font("SemiBold", 22),
               fill=PANEL, anchor="mm")
