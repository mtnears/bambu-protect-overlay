# Multi-Printer Setup

Adding a second (or third, fourth, ...) printer touches three config files. Below is a worked example adding a printer named "Huey" to a single-printer setup that already has "Gort" working.

## 1. `printers.yaml` — add to the list

```yaml
site_label: HOMELAB

printers:
  - name:   Gort
    host:   192.168.1.50
    user:   bblp
    pass:   YOUR_GORT_ACCESS_CODE
    serial: YOUR_GORT_SERIAL

  - name:   Huey                          # NEW
    host:   192.168.1.51
    user:   bblp
    pass:   YOUR_HUEY_ACCESS_CODE
    serial: YOUR_HUEY_SERIAL
```

## 2. `go2rtc.yaml` — add the source, public stream, and drawtext template

```yaml
streams:
  gort_src: rtspx://bblp:YOUR_GORT_ACCESS_CODE@192.168.1.50:322/streaming/live/1
  huey_src: rtspx://bblp:YOUR_HUEY_ACCESS_CODE@192.168.1.51:322/streaming/live/1   # NEW

  gort: ffmpeg:gort_src#video=drawtext=gort
  huey: ffmpeg:huey_src#video=drawtext=huey                                          # NEW

ffmpeg:
  drawtext=gort: -c:v libx264 -profile:v baseline -level:v 4.0 -preset:v veryfast -pix_fmt:v yuv420p -g:v 30 -keyint_min:v 30 -sc_threshold:v 0 -b:v 4M -maxrate:v 5M -bufsize:v 10M -vf "drawbox=x=0:y=ih-180:w=iw:h=180:color=black@0.55:t=fill,drawtext=textfile=/data/overlay/gort_1.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-165,drawtext=textfile=/data/overlay/gort_2.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-128,drawtext=textfile=/data/overlay/gort_3.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-91,drawtext=textfile=/data/overlay/gort_4.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-50"

  # NEW - identical to drawtext=gort but with "huey" everywhere
  drawtext=huey: -c:v libx264 -profile:v baseline -level:v 4.0 -preset:v veryfast -pix_fmt:v yuv420p -g:v 30 -keyint_min:v 30 -sc_threshold:v 0 -b:v 4M -maxrate:v 5M -bufsize:v 10M -vf "drawbox=x=0:y=ih-180:w=iw:h=180:color=black@0.55:t=fill,drawtext=textfile=/data/overlay/huey_1.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-165,drawtext=textfile=/data/overlay/huey_2.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-128,drawtext=textfile=/data/overlay/huey_3.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-91,drawtext=textfile=/data/overlay/huey_4.txt:reload=1:expansion=none:fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:fontsize=26:fontcolor=white:x=20:y=h-50"
```

## 3. `config.yaml` — add a new ONVIF block

```yaml
onvif:
  - mac: 02:00:00:00:00:01                  # YOUR existing Gort MAC
    ports:
      server: 11087
      rtsp: 11057
      snapshot: 11088
    name: Gort
    uuid: 00000000-0000-0000-0000-000000000001    # YOUR existing Gort UUID
    highQuality:
      rtsp: /gort
      snapshot: /api/frame.jpeg?src=gort
      width: 1680
      height: 1080
      framerate: 30
      bitrate: 4096
      quality: 4
    target:
      hostname: 192.168.1.10
      ports:
        rtsp: 8554
        snapshot: 1984

  # NEW - Huey
  - mac: 02:00:00:00:00:02                  # different from Gort, locally administered
    ports:
      server: 11083                          # different port range from Gort
      rtsp: 11055
      snapshot: 11084
    name: Huey
    uuid: 00000000-0000-0000-0000-000000000002    # FRESH uuid - generate with uuidgen
    highQuality:
      rtsp: /huey                            # matches go2rtc stream name
      snapshot: /api/frame.jpeg?src=huey
      width: 1680
      height: 1080
      framerate: 30
      bitrate: 4096
      quality: 4
    target:
      hostname: 192.168.1.10                 # same Docker host as Gort
      ports:
        rtsp: 8554
        snapshot: 1984
```

## 4. Apply

```bash
docker compose down
docker compose up -d --build
```

Wait ~30 seconds, then in UniFi Protect → Devices, the new camera should appear and be ready to adopt.

## Tips

- **MAC addresses** must each be unique AND locally administered. Locally administered means the second nibble of the first byte must be `2`, `6`, `A`, or `E`. So `02:42:...`, `06:F1:...`, `0A:00:...`, `1A:11:...` are all valid; `00:11:22:...` (vendor-assigned) is not.
- **Generate UUIDs** with `uuidgen` (Linux/macOS) or `[guid]::NewGuid()` in PowerShell, or any online UUID v4 generator.
- **Port ranges** — keep each printer in its own clean range. Avoid conflicts with anything else on your host.
- **Stream names** are case-sensitive and must be lowercase. The Python service writes files using lowercase names; the ONVIF wrapper and ffmpeg refer to the same names.

## Performance

Each additional printer adds roughly 5–10% CPU on a Ryzen V1500B-class chip during active streaming. Four simultaneous 1080p30 streams sit at about 25–35% total. If you have many printers and a weaker host, consider:

- Lowering bitrate cap (`-b:v 2M -maxrate:v 3M -bufsize:v 6M`)
- Dropping to 720p with `-vf "scale=1280:720,drawtext=..."` — note this requires reordering the filter chain
- Using a host with hardware-accelerated H.264 encoding (most Intel CPUs with QuickSync) and the `h264_qsv` encoder
