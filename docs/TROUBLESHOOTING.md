# Troubleshooting

Common issues and fixes, in roughly the order they appeared during my own setup.

## Port conflicts on startup

**Symptom:** `bambu-onvif` container exits immediately with:
```
Error: listen EADDRINUSE: address already in use 192.168.1.10:8081
```

**Cause:** Another service on your host (DSM, Sonarr, anything) already owns the port.

**Fix:** Pick port numbers in `config.yaml` that don't conflict with anything else. The `11000–11100` range is generally clean. To find what's already in use on your host:

```bash
sudo ss -tlnH | awk '{print $4}' | awk -F: '{print $NF}' | sort -n -u
```

Pick numbers not in that list.

## Printer `connect failed` in `bambu-overlay` logs

**Symptom:** Log shows:
```
[Gort] connect failed: 7
```

or more revealingly:
```
[Gort] connection error: [Errno 111] Connection refused
```

**Cause:** The printer's port 322 (RTSPS) and 8883 (MQTT) aren't listening. This means **LAN Mode Liveview** is not enabled.

**Fix:**
1. Open Bambu Handy on your phone
2. Tap the printer
3. Settings → General → enable **LAN Mode Liveview**
4. On some firmware revisions you may need to power-cycle the printer for the change to fully take effect

Verify with:
```bash
timeout 3 bash -c "</dev/tcp/<PRINTER_IP>/322" && echo OPEN || echo CLOSED
```

## Camera adopts in UniFi Protect but live view is black

**Symptom:** Camera shows as "Online" with non-zero FPS and bitrate in Protect, but the live viewer is black.

**Cause #1:** Stream Compatibility Mode set incorrectly. Counter-intuitively, **Default** has worked for me; **Improved** has not. Try toggling.

**Cause #2:** Codec mismatch. The `compose.example.yaml` uses `-profile:v baseline` for maximum decoder compatibility. If your Protect viewer still struggles, try lowering further with `-level:v 3.1`.

**Cause #3:** ONVIF wrapper choice. The `kulasolutions/rtsp-to-onvif` image did NOT work with Protect for me. The `daniela-hase/onvif-server` image (used in `compose.example.yaml`) does. If you switched wrappers and it broke, swap back.

## Stream plays but is glitchy/torn

**Symptom:** Top portion of frame renders cleanly, bottom is smeared/distorted.

**Cause:** Encoder settings producing non-conformant H.264. Often caused by `-tune zerolatency` or aggressive presets.

**Fix:** The example `go2rtc.yaml` already uses safe settings (`-preset:v veryfast`, no zerolatency tune, baseline profile, explicit bitrate constraints). If you customized the encoder args, try reverting to the example.

## Overlay text shows but trailing rectangles appear at end of each line

**Symptom:** Overlay renders correctly but small rectangles appear at the right end of each line.

**Cause:** drawtext is rendering newlines as missing-glyph rectangles when `expansion=none` is set.

**Fix:** This is why we use **three separate drawtext filters chained with commas**, one per overlay line. If you collapsed it back into a single drawtext with multi-line text, the rectangles will return. Use the example `go2rtc.yaml` as-is.

## ffmpeg error: "Stray % near"

**Symptom:** `docker logs go2rtc` shows:
```
[Parsed_drawtext_0] Stray % near ')
ETA: ...'
```

**Cause:** `expansion=none` is missing from your drawtext filter. Without it, drawtext interprets `%` as a strftime format escape, and the `(31%)` in the layer count breaks parsing.

**Fix:** Make sure each `drawtext=...` argument in `go2rtc.yaml` includes `expansion=none:` after `reload=1:`.

## ffmpeg error: "Cannot read file '/data/overlay/foo_1.txt'"

**Symptom:** `docker logs go2rtc` shows file-not-found errors.

**Cause:** Either the `bambu-overlay` container isn't running, or the volume mount isn't shared between containers.

**Fix:**
```bash
docker compose ps           # bambu-overlay should be Up
docker exec bambu-overlay ls /data/overlay/   # should show *_1.txt, *_2.txt, *_3.txt
docker exec go2rtc ls /data/overlay/          # should show the SAME files
```

If go2rtc can't see the files, check `compose.yaml` — both `go2rtc` and `bambu-overlay` services need to mount the `overlay-data` named volume.

## Time on overlay is off by hours

**Symptom:** The clock in the overlay is off — usually showing UTC instead of your local time.

**Fix:** Set `TZ=America/Los_Angeles` (or your timezone) on the `bambu-overlay` service in `compose.yaml`. The Dockerfile installs `tzdata` so the env var actually takes effect — if you customized the Dockerfile, make sure `tzdata` is still installed.

After changing TZ:
```bash
docker compose up -d --force-recreate bambu-overlay
docker exec bambu-overlay date
```

## Humidity always shows `-`

**Symptom:** Overlay shows `Humidity: -` even though Bambu Handy shows a real value.

**Cause:** The MQTT schema for AMS humidity varies slightly between printer models. The included parser handles the X1/H2-series schema but may not match older P1/A1 schemas.

**Fix:** Capture an actual MQTT message from your printer and inspect the `print.ams` structure:

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
" | head -200
```

If your humidity lives somewhere other than `print.ams.ams[0].humidity`, please open an issue with the structure (sanitize identifying info first) and I'll add support.

## "no such service: bambu-onvif"

**Symptom:** `docker compose up -d bambu-onvif` returns "no such service".

**Cause:** The service is named `rtsp-to-onvif` in `compose.yaml`. The container_name is `bambu-onvif`. You start it by service name, but you read its logs by container name.

**Fix:**
```bash
docker compose up -d rtsp-to-onvif    # service name (compose syntax)
docker logs bambu-onvif               # container name (docker syntax)
```

## Container restart didn't pick up Python changes

**Symptom:** You edited `bambu_overlay.py`, restarted the container, but the old behavior persists.

**Cause:** `docker compose restart` reuses the existing image. The Python source is COPY'd into the image at build time, so changes to the file on disk don't take effect until rebuild.

**Fix:**
```bash
docker compose up -d --build --force-recreate bambu-overlay
```

## Cameras went offline in Protect after a restart

**Symptom:** Cameras showed as adopted and working, then after some maintenance they all show offline.

**Cause:** Most likely the `rtsp-to-onvif` container isn't running. This can happen if you ran `docker compose up -d <other-service>` while the wrapper was stopped — `up -d <name>` only manages the named service.

**Fix:**
```bash
docker compose ps -a                 # see what's actually running
docker compose down && docker compose up -d   # safest cycle for "everything"
```

In future, prefer `docker compose restart` (no service name = all) when you want to bounce the whole stack.

## Still stuck?

Open an issue with:
- Output of `docker compose ps -a`
- Recent logs from each container (`docker logs --tail 50 <name>`)
- Sanitized version of your config files (remove access codes and serials)
- Bambu printer model and firmware version
- UniFi Protect version and NVR hardware
