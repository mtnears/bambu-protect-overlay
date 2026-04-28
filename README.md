# Bambu Protect Overlay

**Bambu Lab printer cameras in UniFi Protect, with live MQTT data burned into the video.**

Adopt your Bambu printer's camera into UniFi Protect as a third-party camera, and overlay live print data (layer count, ETA, temperatures, filament, AMS humidity, estimated finish time) directly onto the video stream. The overlay is burned into the pixels by ffmpeg, so it appears in both Protect's live view and recordings — perfect for diagnosing failed prints by scrubbing through the recording.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-compose-2496ed.svg)](https://docs.docker.com/compose/)

---

## 🎬 Example overlay

```
MTNEARZ.COM: GORT   Printing                      Apr 28 2026 13:09:09
Layer 31/54 (46%)   ETA 2h 19m (done 15:28)   Nozzle 270/270   Bed 100/100   ASA   Humidity: 3/5 (Normal)
Job: Jig - Bottle Opener
```

Three-line strip across the bottom of the frame, semi-transparent dark background, monospace font, updates every second.

---

## 🌟 Key Features

- **🎥 Real-time camera in UniFi Protect** — adopt as a third-party ONVIF camera, with full live view and continuous recording on your NVR.
- **📊 Live data overlay** — MQTT-driven, burned into pixels with `ffmpeg drawtext`. Visible in live view AND recordings.
- **🕐 Estimated finish time** — calculates current time + remaining minutes so you can see at a glance when a print should be done.
- **💧 Friendly humidity labels** — translates Bambu's 0–5 AMS scale into "Normal", "Damp", "Dry", etc.
- **🏠 Multi-printer ready** — all four of my Bambu printers (Dewey/Huey/Louie/Gort) on one Synology NAS.
- **🔧 Docker-only deployment** — three containers, one compose file, no host-side Python/ffmpeg installs.

---

## 📋 Table of Contents

- [How It Works](#how-it-works)
- [Why Bother?](#why-bother)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Multiple Printers](#multiple-printers)
- [UniFi Protect Adoption](#unifi-protect-adoption)
- [Documentation](#documentation)
- [Troubleshooting](#troubleshooting)
- [Resource Usage](#resource-usage)
- [Acknowledgements](#acknowledgements)
- [License](#license)

---

## 🔧 How It Works

```
Bambu printer ──RTSPS:322──┐
                           ▼
Bambu printer ──MQTT:8883──► bambu-overlay (Python)
                                  │
                                  ▼ writes /data/overlay/*.txt every 1s
                                  │
                  go2rtc + ffmpeg drawtext (reads text files)
                                  │
                                  ▼
                         rtsp-to-onvif (ONVIF wrapper)
                                  │
                                  ▼
                          UniFi Protect (NVR + viewer)
```

Three Docker services running side-by-side on a single host:

1. **`go2rtc`** pulls the encrypted RTSPS video stream from each printer and re-encodes it on the fly with ffmpeg, applying three stacked `drawtext` filters that render live data lines onto the bottom of the frame.
2. **`bambu-overlay`** subscribes to each printer's local MQTT broker and writes formatted text files (one per overlay line per printer) once per second. ffmpeg's `drawtext` reads these files on every frame.
3. **`rtsp-to-onvif`** wraps each printer's processed stream as a virtual ONVIF camera with its own MAC address and IP, so UniFi Protect can discover and adopt them as third-party cameras.

---

## 💡 Why Bother?

Bambu's own monitoring tools only let you review a *fast timelapse* after a failed print. With this setup, your NVR records every frame in real time. When a print fails, you can scrub back, see exactly which layer it failed on, what the nozzle and bed temperatures were, what filament was loaded, and when in the print it happened — all visible in the overlay.

It's also just nice to have all your cameras in one place.

---

## 📦 Requirements

### Hardware
- **A Bambu Lab printer** with **LAN Mode Liveview** enabled. Tested on H2S; should work on X1C, X1E, and other models that expose RTSPS on port 322.
- **A Linux Docker host** on the same LAN as your printers. Tested on Synology DS1621+ (Ryzen V1500B, 4c/8t, 8GB) running 4 simultaneous streams.
- **A UniFi Protect deployment** with **third-party camera support** enabled (currently in beta — Settings → System → "Discover third-party cameras").

### Software
- **Docker 24+** with **Docker Compose v2** (`docker compose ...`)
- **OpenSSL** on the host (only used once during setup to read printer serial numbers from TLS certs)

### Performance notes
Re-encoding 4× 1080p30 software-encoded streams uses about **25–35% total CPU** on a Ryzen V1500B. Lower-end NAS CPUs may struggle with multiple printers — start with one and watch resource usage before adding more.

---

## 🚀 Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/mtnears/bambu-protect-overlay.git
cd bambu-protect-overlay

# 2. Copy the example configs and edit them with your printer details
cp printers.example.yaml printers.yaml
cp go2rtc.example.yaml   go2rtc.yaml
cp config.example.yaml   config.yaml

# Edit each:
#   printers.yaml    - printer IPs, access codes, serials
#   go2rtc.yaml      - RTSPS URLs (matches printers.yaml)
#   config.yaml      - virtual MAC/IP/port assignments per printer
#   compose.yaml     - set TZ for your local timezone

# 3. Enable LAN Mode Liveview on each printer (Bambu Handy → printer → settings)

# 4. Build and start
docker compose up -d --build

# 5. Verify
docker compose ps                              # three services should be running
docker logs bambu-overlay                      # should show "[Gort] connected" etc.
docker exec bambu-overlay cat /data/overlay/gort_1.txt
                                               # should show the rendered overlay text

# 6. In UniFi Protect:
#    Settings -> System -> enable "Discover Third-Party Cameras"
#    Devices -> the cameras appear -> adopt with any username/password
#    (the wrapper doesn't enforce auth)
```

---

## ⚙️ Configuration

### Required: per-printer details (`printers.yaml`)

Three pieces of info per printer:

| Field | How to find it |
|---|---|
| `host` | Printer's local IP. Check Bambu Handy (printer → Settings → WLAN). |
| `pass` | Access Code. Bambu Handy → printer → Settings → WLAN → Access Code. |
| `serial` | Device Serial Number. See below. |

**Finding the serial number** (works while the printer is online with LAN Mode Liveview enabled):

```bash
openssl s_client -connect <PRINTER_IP>:322 -showcerts </dev/null 2>&1 | grep "subject="
# outputs:  subject=CN = XXXXXXXXXXXXXXX
```

The `CN` value is the serial.

### Required: stream and ONVIF setup (`go2rtc.yaml`, `config.yaml`)

Both files have detailed inline comments. The example files are configured for a single printer ("Gort"); to add more, duplicate the relevant blocks and adjust IPs/MACs/ports/UUIDs.

### Required: timezone (`compose.yaml`)

Set `TZ` on the `bambu-overlay` service to your timezone (e.g. `America/Los_Angeles`, `America/New_York`, `Europe/London`). This drives both the wall-clock display and the estimated finish-time calculation.

### Optional: site label (`printers.yaml`)

The text shown at the start of overlay line 1, before the printer name. Defaults to `BAMBU` but most users will want to set their own home/site label.

```yaml
site_label: MYHOME.LOCAL
```

---

## 🖨️ Multiple Printers

To add a second printer, edit three files:

**`printers.yaml`** — add another entry to the `printers:` list with that printer's name, IP, access code, and serial.

**`go2rtc.yaml`** — duplicate the source/public stream pair AND the ffmpeg drawtext template, changing all references from `gort` to your new printer's lowercase name.

**`config.yaml`** — duplicate the `onvif:` block with:
- A unique locally-administered MAC (any address starting with `02`, `06`, `0A`, `0E`, `1A`, etc.)
- Unique port numbers for `server`, `rtsp`, and `snapshot`
- A fresh UUID (`uuidgen` or any online generator)
- The matching stream name in `highQuality.rtsp` and `highQuality.snapshot`

Then `docker compose down && docker compose up -d --build` to apply.

See [docs/MULTI_PRINTER.md](docs/MULTI_PRINTER.md) for a worked example.

---

## 📺 UniFi Protect Adoption

1. **Enable third-party cameras** in UniFi Protect: Settings → System → toggle "Discover Third-Party Cameras". This is currently in beta on Protect 7.x.
2. **Wait for discovery** — within ~30 seconds the cameras appear in Devices as "Onvif Cardinal" or similar.
3. **Adopt** with any username/password (e.g. `admin`/`admin`). The wrapper doesn't enforce auth on the RTSP stream.
4. **Click into the live view** — the overlay should appear within a few seconds of the first keyframe arriving.

If you get a black screen on adoption, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). The most common culprits are the wrong "Stream Compatibility Mode" in Protect's settings, or a network reachability issue between the NVR and the virtual ONVIF IPs.

---

## 📚 Documentation

- **[Installation Guide](docs/INSTALLATION.md)** — step-by-step setup on Synology, including DSM-specific gotchas
- **[Multi-Printer Setup](docs/MULTI_PRINTER.md)** — adding more than one printer with worked examples
- **[Customizing the Overlay](docs/CUSTOMIZING.md)** — change colors, position, font size, fields shown
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** — common issues and fixes

---

## 🛠️ Troubleshooting

Quick triage:

| Symptom | Likely cause |
|---|---|
| `bambu-overlay` shows `connect failed` for a printer | LAN Mode Liveview not enabled on that printer |
| Overlay text file shows but ffmpeg shows "Stray %" | drawtext is interpreting `%` as strftime — `expansion=none` is missing |
| Camera adopted but black screen in Protect | Try toggling Stream Compatibility Mode in Protect settings, then re-adopt |
| Live view fine but stream can't be played in VLC | RTSP transport — VLC defaults to UDP; use TCP |
| Wrapper container immediately exits | Virtual MAC conflict with an existing interface, or invalid UUID format |

Detailed walkthroughs in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## 📊 Resource Usage

On a Synology DS1621+ (Ryzen V1500B, 4c/8t, no hardware video encode) running **four** simultaneous 1080p30 streams with overlay:

- **CPU**: 25–35% total (about one full core)
- **RAM**: under 200 MB across all three containers
- **Disk**: minimal — overlay text files are a few KB; recording space is up to UniFi Protect

For a single printer the CPU cost is negligible (under 10%).

---

## 🙏 Acknowledgements

- [**go2rtc**](https://github.com/AlexxIT/go2rtc) by AlexxIT — the streaming swiss army knife that makes the whole pipeline possible
- [**rtsp-to-onvif**](https://github.com/daniela-hase/onvif-server) by daniela-hase — the ONVIF wrapper that UniFi Protect actually plays nicely with
- The Bambu and UniFi communities for documenting all the schema details and gotchas

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details

Copyright © 2026 Ken Pauley

---

## ⭐ Support This Project

If this saved you a few hours of trial-and-error or rescued a print:

- ⭐ Star the repository
- 🐛 File issues with sanitized configs and logs
- 💡 Share configs that worked for other Bambu models
- 🔧 PRs welcome, especially for additional printer model support and richer overlay layouts

---

**Built for makers who like their NVRs to know what their printers are doing.**
