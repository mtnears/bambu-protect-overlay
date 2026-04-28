#!/usr/bin/env python3
"""
Bambu Printer MQTT Overlay Service

Subscribes to MQTT data from each configured Bambu printer and writes
formatted overlay text files that ffmpeg's drawtext filter reads in real
time. The overlays are burned into the video stream so they appear in
both UniFi Protect's live view and recordings.

Configuration is read from /config/printers.yaml.

For each printer, three files are written (one per overlay line):
  /data/overlay/{name}_1.txt   <- header line (site, name, status, time)
  /data/overlay/{name}_2.txt   <- metrics line (layer, eta, temps, etc.)
  /data/overlay/{name}_3.txt   <- job name line

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
    "line_width": 70,
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
        "printers":   printers,
    }


# ---------------------------------------------------------------------------
# State (initialized after config load)
# ---------------------------------------------------------------------------

state_lock = threading.Lock()
state: dict[str, dict] = {}
SITE_LABEL = ""
LINE_WIDTH = 70


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


# ---------------------------------------------------------------------------
# State extraction
# ---------------------------------------------------------------------------

def update_state(name: str, payload: dict) -> None:
    """Merge a printer's MQTT report payload into our state dictionary."""
    print_data = payload.get("print", {})
    if not isinstance(print_data, dict) or not print_data:
        return

    with state_lock:
        s = state[name]

        if "gcode_state"       in print_data: s["gcode_state"]    = print_data["gcode_state"]
        if "subtask_name"      in print_data: s["subtask_name"]   = print_data["subtask_name"]
        if "mc_percent"        in print_data: s["percent"]        = print_data["mc_percent"]
        if "layer_num"         in print_data: s["layer_num"]      = print_data["layer_num"]
        if "total_layer_num"   in print_data: s["total_layer_num"] = print_data["total_layer_num"]
        if "mc_remaining_time" in print_data: s["remaining"]      = print_data["mc_remaining_time"]

        if "nozzle_temper"        in print_data: s["nozzle"]        = print_data["nozzle_temper"]
        if "nozzle_target_temper" in print_data: s["nozzle_target"] = print_data["nozzle_target_temper"]
        if "bed_temper"           in print_data: s["bed"]           = print_data["bed_temper"]
        if "bed_target_temper"    in print_data: s["bed_target"]    = print_data["bed_target_temper"]

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
                            break


# ---------------------------------------------------------------------------
# Overlay rendering
# ---------------------------------------------------------------------------

def render_lines(name: str, s: dict) -> tuple[str, str, str]:
    """Render the 3 overlay lines for a printer. Returns (line1, line2, line3)."""
    now    = datetime.now()
    clock  = now.strftime("%H:%M:%S")
    date   = now.strftime("%b %d %Y")

    status   = fmt_status(s.get("gcode_state"))
    layer    = fmt_layer(s.get("layer_num"), s.get("total_layer_num"), s.get("percent"))
    eta      = fmt_eta(s.get("remaining"))
    finish   = fmt_finish_time(s.get("remaining"), now)
    nozzle   = fmt_temp_compact(s.get("nozzle"), s.get("nozzle_target"))
    bed      = fmt_temp_compact(s.get("bed"),    s.get("bed_target"))
    filament = s.get("filament") or "-"
    humidity = fmt_humidity(s.get("humidity"))
    job      = s.get("subtask_name") or "-"

    eta_part = f"ETA {eta}" + (f" (done {finish})" if finish else "")

    left  = f"{SITE_LABEL}: {name.upper()}   {status}"
    right = f"{date} {clock}"
    pad   = max(LINE_WIDTH - len(left) - len(right), 2)
    line1 = left + (" " * pad) + right

    line2 = (
        f"Layer {layer}   {eta_part}   "
        f"Nozzle {nozzle}   Bed {bed}   "
        f"{filament}   Humidity: {humidity}"
    )
    line3 = f"Job: {job}"

    return line1.rstrip(), line2.rstrip(), line3.rstrip()


def write_overlay_files(name: str) -> None:
    """Write three files per printer, one per overlay line, atomically."""
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
            except Exception as exc:
                log.warning("[%s] write error: %s", p["name"], exc)
        time.sleep(WRITE_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global SITE_LABEL, LINE_WIDTH, state

    cfg = load_config()
    SITE_LABEL = cfg["site_label"]
    LINE_WIDTH = cfg["line_width"]
    printers   = cfg["printers"]

    state = {p["name"]: {} for p in printers}

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
