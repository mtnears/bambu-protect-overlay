#!/usr/bin/env python3
"""
Bambu Printer MQTT Overlay Service

Subscribes to MQTT data from each configured Bambu printer and writes
formatted overlay text files that ffmpeg's drawtext filter reads in real
time. The overlays are burned into the video stream so they appear in
both UniFi Protect's live view and recordings.

Configuration is read from /config/printers.yaml.

For each printer, four files are written (one per overlay line):
  /data/overlay/{name}_1.txt   <- header line (site, name, status, time)
  /data/overlay/{name}_2.txt   <- progress + temps line
  /data/overlay/{name}_3.txt   <- filament + speed + stage line
  /data/overlay/{name}_4.txt   <- job name line

Each line is rendered by a separate drawtext filter at a different y
position, which avoids newline/escape complexity in ffmpeg.

Update rate: once per second per printer.
"""

import json
import logging
import os
import ssl
import sys
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH    = Path(os.environ.get("CONFIG_PATH", "/config/printers.yaml"))
OUTPUT_DIR     = Path(os.environ.get("OUTPUT_DIR", "/data/overlay"))
PORT           = 8883
WRITE_INTERVAL = 1.0  # seconds

# Defaults if not set in printers.yaml
DEFAULTS = {
    "site_label": "BAMBU",
    "line_width": 80,
    "job_width": 100,
    "makerworld_titles": True,
    "overlay_style": "text",      # "text" (drawtext lines) or "hud" (graphical, see hud.py)
    "logo": None,                 # HUD: path inside the container to a logo PNG
    "video_size": "1920x1080",    # HUD: printer stream size; per-printer override allowed
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bambu-overlay")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load and validate printers.yaml."""
    if not CONFIG_PATH.exists():
        log.error("config not found: %s", CONFIG_PATH)
        log.error("see printers.example.yaml for the expected format")
        sys.exit(1)

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except Exception as exc:
        log.error("failed to parse %s: %s", CONFIG_PATH, exc)
        sys.exit(1)

    if not isinstance(cfg, dict):
        log.error("config must be a YAML mapping (printers.yaml is malformed)")
        sys.exit(1)

    printers = cfg.get("printers")
    if not isinstance(printers, list) or not printers:
        log.error("config must contain a non-empty 'printers' list")
        sys.exit(1)

    required_keys = {"name", "host", "user", "pass", "serial"}
    for p in printers:
        missing = required_keys - set(p)
        if missing:
            log.error("printer entry missing keys %s: %s", sorted(missing), p)
            sys.exit(1)

    return {
        "site_label": cfg.get("site_label", DEFAULTS["site_label"]),
        "line_width": int(cfg.get("line_width", DEFAULTS["line_width"])),
        "job_width":  int(cfg.get("job_width", DEFAULTS["job_width"])),
        "makerworld_titles": bool(cfg.get("makerworld_titles", DEFAULTS["makerworld_titles"])),
        "overlay_style": str(cfg.get("overlay_style", DEFAULTS["overlay_style"])).lower(),
        "logo": cfg.get("logo", DEFAULTS["logo"]),
        "video_size": str(cfg.get("video_size", DEFAULTS["video_size"])),
        "printers":   printers,
    }


# ---------------------------------------------------------------------------
# State (initialized after config load)
# ---------------------------------------------------------------------------

state_lock = threading.Lock()
state: dict[str, dict] = {}
SITE_LABEL = ""
LINE_WIDTH = 80
JOB_WIDTH  = 100
DISPLAY_NAMES: dict[str, str] = {}   # internal name -> name shown on line 1
OVERLAY_STYLE = "text"
LOGO_PATH: str | None = None
VIDEO_SIZES: dict[str, tuple[int, int]] = {}   # internal name -> (w, h) for the HUD image


def _parse_size(v: str) -> tuple[int, int]:
    w, h = str(v).lower().split("x")
    return int(w), int(h)


# ---------------------------------------------------------------------------
# Type-safe coercion
# ---------------------------------------------------------------------------

def _to_int(v):
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_str(v):
    return None if v is None else str(v)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def fmt_eta(minutes) -> str:
    m = _to_int(minutes)
    if m is None or m < 0:
        return "-"
    h, mm = divmod(m, 60)
    return f"{h}h {mm:02d}m" if h else f"{mm}m"


def fmt_finish_time(minutes, now: datetime):
    m = _to_int(minutes)
    if m is None or m <= 0:
        return None
    return (now + timedelta(minutes=m)).strftime("%H:%M")


def fmt_temp_compact(actual, target) -> str:
    a = _to_float(actual)
    t = _to_float(target)
    if a is None and t is None:
        return "-/-"
    a_str = f"{a:.0f}" if a is not None else "-"
    # Target 0 means the heater is off (idle/finished); "33/0" reads like a fault.
    if t is not None and t <= 0:
        return f"{a_str} (off)"
    t_str = f"{t:.0f}" if t is not None else "-"
    return f"{a_str}/{t_str}"


def fmt_status(gcode_state) -> str:
    s = _to_str(gcode_state)
    if not s:
        return "-"
    return {
        "RUNNING":  "Printing",
        "FINISH":   "Finished",
        "IDLE":     "Idle",
        "PAUSE":    "Paused",
        "FAILED":   "Failed",
        "PREPARE":  "Preparing",
    }.get(s, s.title())


# Bambu AMS humidity is a 0-5 integer.  These labels match the Bambu
# Handy app's interpretation.
HUMIDITY_LABELS = {
    1: "Wet",
    2: "Damp",
    3: "Normal",
    4: "Dry",
    5: "Very Dry",
}


def fmt_humidity(level) -> str:
    n = _to_int(level)
    if n is None:
        return "-"
    label = HUMIDITY_LABELS.get(n)
    return f"{n}/5 ({label})" if label else f"{n}/5"


def fmt_layer(layer, total, percent) -> str:
    l = _to_int(layer)
    t = _to_int(total)
    p = _to_int(percent)
    if l is None or t is None:
        return "-"
    pct = f" ({p}%)" if p is not None else ""
    return f"{l}/{t}{pct}"


# Bambu's print speed-level enum (set on the printer or via slicer).
# spd_mag is a numeric multiplier - 100 = Standard, 166 = Ludicrous, etc.
SPEED_LEVELS = {
    1: "Silent",
    2: "Standard",
    3: "Sport",
    4: "Ludicrous",
}


def fmt_speed(level, magnitude) -> str:
    n = _to_int(level)
    label = SPEED_LEVELS.get(n) if n is not None else None
    mag = _to_int(magnitude)
    if label and mag is not None:
        return f"{label} ({mag}%)"
    if label:
        return label
    if mag is not None:
        return f"{mag}%"
    return "-"


def fmt_chamber(temp) -> str:
    """Chamber temperature in Celsius. Returns '-' for printers that don't
    expose chamber temp (X1C, P1S, A1, A1 Mini)."""
    n = _to_int(temp)
    if n is None:
        return "-"
    return f"{n}"


# Print stage codes - Bambu's internal enum for the various phases of
# a print job. List built up from various community-documented values
# plus observation in real captures.
STAGE_CODES = {
    -1: "Idle",
    0:  "Printing",
    1:  "Auto Bed Leveling",
    2:  "Heatbed Preheating",
    3:  "Sweeping XY",
    4:  "Changing Filament",
    5:  "Slicer Pause",
    6:  "Filament Runout",
    7:  "Heating Hotend",
    8:  "Calibrating Extrusion",
    9:  "Scanning Bed Surface",
    10: "Inspecting First Layer",
    11: "Identifying Build Plate",
    12: "Calibrating Lidar",
    13: "Homing Toolhead",
    14: "Cleaning Nozzle",
    15: "Checking Hotend Temp",
    16: "Paused (User)",
    17: "Paused (Cover)",
    18: "Calibrating Lidar",
    19: "Calibrating Flow",
    20: "Paused (Hotend Temp)",
    21: "Paused (Bed Temp)",
    22: "Unloading Filament",
    23: "Skip-Step Pause",
    24: "Loading Filament",
    25: "Calibrating Motor Noise",
    26: "Paused (AMS Lost)",
    27: "Paused (Heatbreak Fan)",
    28: "Paused (Chamber Temp)",
    29: "Cooling Chamber",
    30: "Paused (Gcode)",
    31: "Motor Noise Showoff",
    32: "Paused (Nozzle Cover)",
    33: "Paused (Cutter Error)",
    34: "Paused (First Layer)",
    35: "Paused (Nozzle Clog)",
    74: "Heating Bed",
    255: "-",
}


def fmt_stage(stg_cur) -> str:
    n = _to_int(stg_cur)
    if n is None:
        return "-"
    return STAGE_CODES.get(n, f"Stage {n}")


# ---------------------------------------------------------------------------
# State extraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# MakerWorld model title lookup
# ---------------------------------------------------------------------------
# Prints started from MakerWorld / Bambu Handy report the print *profile* as
# subtask_name (e.g. "0.2mm layer, 2 walls, 25% infill") but also carry the
# MakerWorld design_id. The public design API turns that into the model's
# title. Lookups run in a background thread and are cached, so a slow or
# unreachable MakerWorld never stalls the overlay. Set makerworld_titles: false
# in printers.yaml to disable.

MAKERWORLD_API = "https://makerworld.com/api/v1/design-service/design/{}"
MAKERWORLD_TITLES = True
MAKERWORLD_RETRY = 600             # seconds before a failed or empty lookup is tried again
_design_titles: dict = {}          # design_id -> (title, time of lookup); "" = no title
_design_pending: set = set()       # design_ids with a lookup in flight
_design_lock = threading.Lock()


def _fetch_design_title(design_id: str) -> None:
    title = ""
    try:
        req = urllib.request.Request(MAKERWORLD_API.format(design_id),
                                     headers={"User-Agent": "bambu-protect-overlay"})
        with urllib.request.urlopen(req, timeout=10) as r:
            title = str(json.load(r).get("title") or "").strip()
    except Exception as e:  # network, HTTP or JSON errors: fall back to subtask_name
        log.info("MakerWorld title lookup for design %s failed: %s", design_id, e)
    with _design_lock:
        _design_titles[design_id] = (title, time.monotonic())
        _design_pending.discard(design_id)
    if title:
        log.info("MakerWorld design %s: %s", design_id, title)


def design_title(design_id) -> str | None:
    """Cached MakerWorld title for design_id; starts a background lookup on first use."""
    d = _to_str(design_id)
    if not MAKERWORLD_TITLES or not d or d == "0":
        return None
    with _design_lock:
        cached = _design_titles.get(d)
        if cached and (cached[0] or time.monotonic() - cached[1] < MAKERWORLD_RETRY):
            return cached[0] or None
        if d in _design_pending:    # a lookup is already running
            return cached[0] if cached and cached[0] else None
        _design_pending.add(d)
    threading.Thread(target=_fetch_design_title, args=(d,), daemon=True).start()
    return None


def _printable(text: str) -> str:
    """Drop characters the overlay font can't draw (emoji, pictographs, joiners)
    and collapse whitespace. MakerWorld titles often contain emoji, which
    drawtext would render as empty boxes."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF or 0x2600 <= cp <= 0x27BF or cp in (0x200D, 0xFE0F, 0xFE0E):
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 3, 1)].rstrip() + "..."


def fmt_job(subtask_name, design_id, width: int = 0) -> str:
    """Model title (from MakerWorld) plus the task/profile name, or just the task name.
    Trimmed to `width` characters: the task name in parentheses goes first, then the
    title is shortened, so the line never runs off the edge of the frame."""
    task = _printable(_to_str(subtask_name) or "")
    title = _printable(design_title(design_id) or "")
    if title and task and task.lower() != title.lower():
        job = f"{title}  ({task})"
        if width and len(job) > width:
            job = title
    else:
        job = title or task or "-"
    return _fit(job, width) if width else job


def update_state(name: str, payload: dict) -> None:
    """Merge a printer's MQTT report payload into our state dictionary."""
    print_data = payload.get("print", {})
    if not isinstance(print_data, dict) or not print_data:
        return

    with state_lock:
        s = state[name]

        if "gcode_state"       in print_data: s["gcode_state"]    = print_data["gcode_state"]
        if "subtask_name"      in print_data: s["subtask_name"]   = print_data["subtask_name"]
        if "design_id"         in print_data: s["design_id"]      = print_data["design_id"]
        if "mc_percent"        in print_data: s["percent"]        = print_data["mc_percent"]
        if "layer_num"         in print_data: s["layer_num"]      = print_data["layer_num"]
        if "total_layer_num"   in print_data: s["total_layer_num"] = print_data["total_layer_num"]
        if "mc_remaining_time" in print_data: s["remaining"]      = print_data["mc_remaining_time"]

        if "nozzle_temper"        in print_data: s["nozzle"]        = print_data["nozzle_temper"]
        if "nozzle_target_temper" in print_data: s["nozzle_target"] = print_data["nozzle_target_temper"]
        if "bed_temper"           in print_data: s["bed"]           = print_data["bed_temper"]
        if "bed_target_temper"    in print_data: s["bed_target"]    = print_data["bed_target_temper"]

        # Chamber temperature (H2-series and newer; falls back to None on older models)
        # Two equivalent paths in the schema - try both.
        chamber = None
        ctc = print_data.get("device", {}).get("ctc", {}).get("info", {})
        if isinstance(ctc, dict) and "temp" in ctc:
            chamber = ctc["temp"]
        else:
            info_obj = print_data.get("info")
            if isinstance(info_obj, dict) and "temp" in info_obj:
                chamber = info_obj["temp"]
        if chamber is not None:
            s["chamber"] = chamber

        # Print speed setting (Silent/Standard/Sport/Ludicrous + numeric multiplier)
        if "spd_lvl" in print_data: s["spd_lvl"] = print_data["spd_lvl"]
        if "spd_mag" in print_data: s["spd_mag"] = print_data["spd_mag"]

        # Print stage (current activity within a print job - see STAGE_CODES)
        if "stg_cur" in print_data: s["stg_cur"] = print_data["stg_cur"]

        # AMS: humidity + active filament
        # Schema: print.ams.ams[N].{humidity, tray[]}
        # Active tray identified by print.ams.tray_now (string ID).
        ams_outer = print_data.get("ams")
        if isinstance(ams_outer, dict):
            tray_now = _to_str(ams_outer.get("tray_now"))
            ams_units = ams_outer.get("ams") or []
            if isinstance(ams_units, list) and ams_units:
                active_unit = None
                for unit in ams_units:
                    if not isinstance(unit, dict):
                        continue
                    trays = unit.get("tray") or []
                    if any(_to_str(t.get("id")) == tray_now for t in trays if isinstance(t, dict)):
                        active_unit = unit
                        break
                if active_unit is None and isinstance(ams_units[0], dict):
                    active_unit = ams_units[0]

                if active_unit:
                    if "humidity" in active_unit:
                        s["humidity"] = active_unit["humidity"]
                    for tray in active_unit.get("tray") or []:
                        if not isinstance(tray, dict):
                            continue
                        if _to_str(tray.get("id")) == tray_now:
                            tt = tray.get("tray_type")
                            if tt:
                                s["filament"] = tt
                            tc = _to_str(tray.get("tray_color"))
                            if tc and len(tc) >= 6:
                                s["filament_color"] = tc[:6]
                            break


# ---------------------------------------------------------------------------
# Overlay rendering
# ---------------------------------------------------------------------------

def render_lines(name: str, s: dict) -> tuple[str, str, str, str]:
    """Render the 4 overlay lines for a printer. Returns (line1, line2, line3, line4)."""
    now    = datetime.now()
    clock  = now.strftime("%H:%M:%S")
    date   = now.strftime("%b %d %Y")

    status   = fmt_status(s.get("gcode_state"))
    layer    = fmt_layer(s.get("layer_num"), s.get("total_layer_num"), s.get("percent"))
    eta      = fmt_eta(s.get("remaining"))
    finish   = fmt_finish_time(s.get("remaining"), now)
    nozzle   = fmt_temp_compact(s.get("nozzle"), s.get("nozzle_target"))
    bed      = fmt_temp_compact(s.get("bed"),    s.get("bed_target"))
    chamber  = fmt_chamber(s.get("chamber"))
    filament = s.get("filament") or "-"
    humidity = fmt_humidity(s.get("humidity"))
    speed    = fmt_speed(s.get("spd_lvl"), s.get("spd_mag"))
    stage    = fmt_stage(s.get("stg_cur"))
    job      = fmt_job(s.get("subtask_name"), s.get("design_id"), max(JOB_WIDTH - len("Job: "), 10))

    eta_part = f"ETA {eta}" + (f" (done {finish})" if finish else "")

    # Line 1: site, name, status (left)  +  date and clock (right)
    left  = f"{SITE_LABEL}: {DISPLAY_NAMES.get(name, name).upper()}   {status}"
    right = f"{date} {clock}"
    pad   = max(LINE_WIDTH - len(left) - len(right), 2)
    line1 = left + (" " * pad) + right

    # Line 2: print progress + temps (chamber added)
    line2 = (
        f"Layer {layer}   {eta_part}   "
        f"Nozzle {nozzle}   Bed {bed}   Chamber {chamber}"
    )

    # Line 3: filament + humidity + speed + stage
    line3 = (
        f"{filament}   Humidity: {humidity}   "
        f"Speed: {speed}   Stage: {stage}"
    )

    # Line 4: job name
    line4 = f"Job: {job}"

    return line1.rstrip(), line2.rstrip(), line3.rstrip(), line4.rstrip()


def build_view(name: str, s: dict) -> dict:
    """Translate raw MQTT state into the plain dict hud.render() draws."""
    task = _printable(_to_str(s.get("subtask_name")) or "")
    title = _printable(design_title(s.get("design_id")) or "")
    job, profile = (title, task if task.lower() != title.lower() else "") if title else (task, "")
    color = None
    fc = s.get("filament_color")
    if fc:
        try:
            color = tuple(int(fc[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            color = None
    hn = _to_int(s.get("humidity"))
    lvl = _to_int(s.get("spd_lvl"))
    return {
        "display": DISPLAY_NAMES.get(name, name),
        "status": fmt_status(s.get("gcode_state")),
        "stage": fmt_stage(s.get("stg_cur")) if s.get("stg_cur") is not None else "",
        "job": job or None,
        "profile": profile or None,
        "percent": _to_int(s.get("percent")) or 0,
        "layer": _to_int(s.get("layer_num")) or 0,
        "total_layers": _to_int(s.get("total_layer_num")) or 0,
        "remaining": _to_int(s.get("remaining")),
        "nozzle": _to_float(s.get("nozzle")), "nozzle_t": _to_float(s.get("nozzle_target")),
        "bed": _to_float(s.get("bed")), "bed_t": _to_float(s.get("bed_target")),
        "chamber": _to_float(s.get("chamber")), "chamber_t": None,
        "filament": s.get("filament"), "filament_color": color,
        "speed_mag": _to_int(s.get("spd_mag")),
        "speed_level": SPEED_LEVELS.get(lvl, "") if lvl is not None else "",
        "humidity_n": hn, "humidity_label": HUMIDITY_LABELS.get(hn, "") if hn is not None else "",
        "pauses": [],
        "logo": LOGO_PATH,
        "site_label": SITE_LABEL,
    }


def write_hud_file(name: str) -> None:
    """Render the graphical HUD and replace <name>_hud.png atomically."""
    import hud   # imported lazily: Pillow is only needed for the HUD style
    with state_lock:
        view = build_view(name, dict(state[name]))
    img = hud.render(view, VIDEO_SIZES.get(name, (1920, 1080)))
    if view["status"] == "Paused":
        hud.paused_banner(img, view["layer"], None)
    path = OUTPUT_DIR / f"{name.lower()}_hud.png"
    tmp = path.with_suffix(".tmp.png")
    img.save(tmp, compress_level=1)
    os.replace(tmp, path)


def write_overlay_files(name: str) -> None:
    """Write four files per printer, one per overlay line, atomically."""
    with state_lock:
        lines = render_lines(name, state[name])

    base = name.lower()
    for idx, line in enumerate(lines, start=1):
        path = OUTPUT_DIR / f"{base}_{idx}.txt"
        tmp  = path.with_suffix(".tmp")
        tmp.write_text(line, encoding="utf-8", newline="\n")
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# MQTT plumbing
# ---------------------------------------------------------------------------

def make_client(printer: dict) -> mqtt.Client:
    name   = printer["name"]
    serial = printer["serial"]
    topic  = f"device/{serial}/report"

    client = mqtt.Client(
        client_id=f"bambu-overlay-{name.lower()}",
        protocol=mqtt.MQTTv311,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.username_pw_set(printer["user"], printer["pass"])

    # Bambu uses self-signed certs - accept them
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("[%s] connected", name)
            c.subscribe(topic, qos=0)
            # Ask for a full status report: printers otherwise send only changed
            # fields, so the job name etc. would stay blank until the next change.
            c.publish(f"device/{serial}/request",
                      json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}), qos=0)
        else:
            log.warning("[%s] connect failed: %s", name, reason_code)

    def on_disconnect(c, userdata, flags, reason_code, properties):
        log.info("[%s] disconnected (%s)", name, reason_code)

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
            update_state(name, payload)
        except Exception as exc:
            log.warning("[%s] payload parse error: %s", name, exc)

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def run_printer_thread(printer: dict) -> None:
    name = printer["name"]
    while True:
        try:
            client = make_client(printer)
            client.connect(printer["host"], PORT, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:
            log.error("[%s] connection error: %s", name, exc)
            time.sleep(5)


def writer_thread(printers: list) -> None:
    """Single thread that writes all overlay files once per second."""
    while True:
        for p in printers:
            try:
                write_overlay_files(p["name"])
                if OVERLAY_STYLE == "hud":
                    write_hud_file(p["name"])
            except Exception as exc:
                log.warning("[%s] write error: %s", p["name"], exc)
        time.sleep(WRITE_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global SITE_LABEL, LINE_WIDTH, JOB_WIDTH, MAKERWORLD_TITLES, DISPLAY_NAMES, state
    global OVERLAY_STYLE, LOGO_PATH, VIDEO_SIZES

    cfg = load_config()
    SITE_LABEL = cfg["site_label"]
    LINE_WIDTH = cfg["line_width"]
    JOB_WIDTH  = cfg["job_width"]
    MAKERWORLD_TITLES = cfg["makerworld_titles"]
    printers   = cfg["printers"]

    state = {p["name"]: {} for p in printers}
    # Optional "display" key: the name shown on the overlay. The internal "name" stays
    # the key for overlay file names, go2rtc streams and ONVIF config, so a printer can
    # be renamed on screen (e.g. a replacement unit "Huey.v2") without re-adopting it.
    DISPLAY_NAMES = {p["name"]: str(p.get("display") or p["name"]) for p in printers}
    OVERLAY_STYLE = cfg["overlay_style"]
    LOGO_PATH = cfg["logo"]
    VIDEO_SIZES = {p["name"]: _parse_size(p.get("video_size") or cfg["video_size"]) for p in printers}
    if OVERLAY_STYLE == "hud":
        log.info("overlay style: hud (writing <name>_hud.png; logo: %s)", LOGO_PATH or "none")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("output dir: %s", OUTPUT_DIR)
    log.info("site label: %s", SITE_LABEL)
    log.info("local time: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"))
    log.info("printers:   %s", ", ".join(p["name"] for p in printers))

    # Pre-populate empty overlay files so ffmpeg has something to read
    for p in printers:
        write_overlay_files(p["name"])

    # One MQTT thread per printer
    for p in printers:
        t = threading.Thread(
            target=run_printer_thread,
            args=(p,),
            daemon=True,
            name=f"mqtt-{p['name'].lower()}",
        )
        t.start()

    # File writer thread (foreground)
    writer_thread(printers)


if __name__ == "__main__":
    main()
