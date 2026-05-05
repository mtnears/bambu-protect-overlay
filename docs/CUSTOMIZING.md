# Customizing the Overlay

## What you can change without touching code

These are all in `go2rtc.yaml`'s `drawtext=<name>:` filter strings.

| What | Where | Notes |
|---|---|---|
| **Position** | `x=20:y=h-N` | `x=20` = 20px from left, `y=h-50` = 50px from bottom. To anchor top-left: `x=20:y=20`. To anchor top-right: `x=w-tw-20:y=20`. |
| **Font size** | `fontsize=26` | Larger numbers = larger text. 26px is readable on 1080p video and in mobile Protect tiles. |
| **Text color** | `fontcolor=white` | Any X11 color name or `0xRRGGBB`. Try `lime`, `yellow`, or `0x00FFCC`. |
| **Background bar** | `drawbox=x=0:y=ih-180:w=iw:h=180:color=black@0.55:t=fill` | Single semi-transparent rectangle behind the text. `@0.55` is alpha (0=invisible, 1=solid). Adjust `h=180` to make the bar taller/shorter; if you change it, also adjust the `y=h-N` values for each `drawtext` to keep the lines inside the bar. |
| **Font face** | `fontfile=/usr/share/fonts/droid/DroidSansMono.ttf` | Pre-installed in the go2rtc image. Use `docker exec go2rtc find / -name "*.ttf"` to see what else is available. |

After editing `go2rtc.yaml`, restart go2rtc only:

```bash
docker compose restart go2rtc
```

No re-adoption in Protect needed — the camera-level params (resolution, framerate, codec) didn't change.

## Changing what data is shown

Edit `bambu-overlay/bambu_overlay.py`. Look for the `render_lines()` function (around line 270). It returns a tuple of `(line1, line2, line3)`.

For example, to add fan speed to line 2, you'd:

1. Add the field extraction in `update_state()`:
   ```python
   if "cooling_fan_speed" in print_data: s["fan"] = print_data["cooling_fan_speed"]
   ```

2. Add a formatter:
   ```python
   def fmt_fan(speed) -> str:
       n = _to_int(speed)
       return f"{n*10}%" if n is not None else "-"
   ```

3. Include it in line 2:
   ```python
   line2 = (
       f"Layer {layer}   {eta_part}   "
       f"Nozzle {nozzle}   Bed {bed}   Fan {fmt_fan(s.get('fan'))}   "
       f"{filament}   Humidity: {humidity}"
   )
   ```

After editing the Python source, you must rebuild:

```bash
docker compose up -d --build --force-recreate bambu-overlay
```

To find what other fields are available, capture an MQTT message and inspect:

```bash
docker exec bambu-overlay python3 -c "
import paho.mqtt.client as mqtt, ssl, json, sys
def on_message(c,u,m):
    print(json.dumps(json.loads(m.payload), indent=2)); sys.exit(0)
c = mqtt.Client(client_id='probe', callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
c.username_pw_set('bblp', '<ACCESS_CODE>')
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
c.tls_set_context(ctx)
c.on_message = on_message
c.connect('<PRINTER_IP>', 8883)
c.subscribe('device/<SERIAL>/report')
c.loop_forever()
"
```

The `print:` object is where most useful fields live.

## Performance Tuning

Software H.264 encoding with the drawtext overlay is the most CPU-intensive part of this stack. The defaults (30fps, `veryfast` preset, 4 Mbps) prioritize quality and motion smoothness, which works fine for 1-2 printers on most hardware. With more printers or weaker hardware, you may want to dial back.

The relevant flags in each `drawtext=NAME` template in `go2rtc.yaml`:

| Flag | Default | Lower-CPU option | Effect |
|---|---|---|---|
| `-r` | `30` | `15` or `10` | Output framerate. Halving roughly halves encode CPU. 10fps is plenty for a print bed; 15fps is a good middle ground. |
| `-preset:v` | `veryfast` | `ultrafast` | x264 speed preset. `ultrafast` is roughly 2x faster than `veryfast` with a slight quality trade-off. |
| `-b:v` / `-maxrate:v` / `-bufsize:v` | `4M / 5M / 10M` | `2M / 3M / 6M` | Bitrate cap. Lower means less encode work AND lower bandwidth. 2 Mbps is plenty for printer cam + readable overlay. |
| `-g:v` / `-keyint_min:v` | `30 / 30` | match your `-r` value | Keyframe interval. Should equal your `-r` (one keyframe per second). |

### Reference numbers

From a Synology DS1621+ (Ryzen V1500B, 4c/8t, software encoding) running **four** simultaneous 1080p streams with overlay:

| Profile | Settings | CPU usage |
|---|---|---|
| Quality (defaults) | 30fps / veryfast / 4 Mbps | ~415% (52% of host) |
| Balanced | 15fps / veryfast / 3 Mbps | ~280% (35% of host) |
| Lightweight | 10fps / ultrafast / 2 Mbps | ~210% (26% of host) |

Single-printer setups will be ~25% of the above. CPU usage is roughly linear with printer count.

### When to tune

- **1-2 printers, modern CPU** — leave the defaults alone.
- **3+ printers, mid-range CPU** — try the balanced profile (15fps) first.
- **4+ printers OR ARM/Atom CPU** — use the lightweight profile.

After changing settings, restart go2rtc only (no Protect re-adoption needed):

```bash
docker compose restart go2rtc
docker stats --no-stream go2rtc
```

## Adding more overlay lines

The current code writes four text files per printer (`<name>_1.txt`, `_2.txt`, `_3.txt`, `_4.txt`) and `go2rtc.yaml` chains four drawtext filters per stream. To add a fifth line:

1. In `bambu_overlay.py`, change `render_lines()` to return five strings, and update `write_overlay_files()` to iterate over `(1, 2, 3, 4, 5)` accordingly.

2. In `go2rtc.yaml`, add a fifth `drawtext=...` to each printer's chain, with a different `y=` value (e.g. `y=h-200` to stack above the existing four). Also bump the `drawbox` `h=180` to `h=220` and `y=ih-180` to `y=ih-220` so the background bar covers the new line.

3. Rebuild bambu-overlay and restart go2rtc.

## Hardware acceleration

The example uses software H.264 encoding (`-c:v libx264`). On hosts with Intel QuickSync, you can switch to `-c:v h264_qsv` for ~5x throughput improvement. AMD VAAPI users can try `-c:v h264_vaapi` (more setup required). Encoder availability varies by ffmpeg build — verify with:

```bash
docker exec go2rtc ffmpeg -encoders 2>/dev/null | grep h264
```

If hardware encoders are listed, you can swap in their flags. Note that the additional `-init_hw_device` and `-vaapi_device` arguments may also be needed for VAAPI.
