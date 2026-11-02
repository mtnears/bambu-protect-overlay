# Shop view: printers on a browser or TV

A web page that shows your printers on any browser or smart TV: live camera tiles with HUD-style dials, the printer's full HUD when one job is running, and a clock screensaver when nothing is printing. It needs no keyboard or remote; the layout follows the jobs on its own.

It is independent of UniFi Protect. Run the Protect path, the shop view, or both:

| | UniFi Protect path | Shop view |
|---|---|---|
| What you get | HUD burned into the video, adopted by Protect as ONVIF cameras | a page at `http://<host>:8130/shop` |
| Services | go2rtc, bambu-overlay, rtsp-to-onvif (+ macvlans) | go2rtc, bambu-overlay, shop-view |
| Overlay style | `hud` (or `text`) | any; `hud` adds the full-screen HUD for one job |

## What it shows

- **Nothing printing:** a large clock and date that drift slowly (a screensaver), and a card per printer with its loaded filament.
- **1 job:** that printer's HUD stream full screen (with `overlay_style: hud`), otherwise its camera full screen with dials.
- **2 jobs:** side by side. **3:** one large and two small. **4:** 2x2. **5:** 3x2 with the clock in the sixth cell.
- Each tile: name, status (or the current stage, e.g. "Auto Bed Leveling"), job, progress bar with slicer-pause markers, and dials for layer, time remaining (with finish time), nozzle, bed and chamber (current, with a tick at the target).
- An amber frame and banner while a printer is paused (with the pause note), a countdown before an upcoming slicer pause, and a green "Done" (or red "Failed") frame that stays for 15 minutes after a job ends.
- A red banner if the status feed stops updating.

## How it works

- `bambu-overlay` writes `status.json` next to its overlay images once a second: each printer's state, job, progress, temperatures and targets, filament, next slicer pause, and `site_label`. It contains no printer addresses, serials or access codes.
- The `shop-view` service (nginx) serves the page and `status.json`, and passes the page's video requests through to go2rtc on the same address. Only the player files, still frames, MJPEG and the video websocket are passed through; go2rtc's stream list, which contains the access codes, is not.
- The page is a fixed 1920x1080 screen scaled to the browser window, so a laptop and a TV show the same picture.

## Setup

1. Use the compose service: `docker compose --profile shop-view up -d`. It runs with host networking on port **8130**. Change `listen 8130` in `shop-view/nginx.conf` for another port.
2. `printers.yaml`: printers without a camera stream in go2rtc get `camera: false` (they show "Camera off"). `display` names and `site_label` are used on the page too (a trailing `.com`, `.net` or `.org` is dropped from the label).
3. Camera tiles play the `<name>_src` streams; the one-job HUD plays the `<name>` stream. For TV browsers, add the optional MJPEG copies to `go2rtc.yaml` (see below).
4. Open `http://<host>:8130/shop`.

## Browsers and TVs (tested November 2026)

| Browser | Video | Notes |
|---|---|---|
| Edge / Chrome / Firefox on a computer | H.264 over **WebRTC** | MSE (go2rtc's default) stalled in Edge with these streams; WebRTC plays. |
| Fire TV (Silk, Chrome 131) | WebRTC for up to **2** camera tiles, MJPEG for 3+ | The Fire TV's decoder handles two live H.264 streams; a third stays black. The page switches automatically. Press OK once for full screen. |
| LG TV browser (NetCast, Chrome 79) | MJPEG | The page avoids newer CSS (inset, aspect-ratio, flexbox gap, container units) so it renders on older TV browsers. |
| Other smart-TV browsers (webOS, Tizen, BRAVIA, HbbTV) | MJPEG | Detected by user agent. |

MJPEG copies for TV browsers (`go2rtc.yaml`, on demand only, so ffmpeg runs only while a screen watches):
```yaml
  gort_mjpeg: ffmpeg:gort_src#video=mjpeg#width=960     # camera tiles
  gort_hudmjpeg: ffmpeg:gort#video=mjpeg                # one job full screen (HUD)
```
These run on the CPU: about 20 % of one core per 960-wide tile while watched (Intel Core Ultra 5 125H). A GPU (`#hardware`) version failed on a pixel-format mismatch.

**Fire TV tips:** turn the screensaver off (Settings > Display & Sounds > Screen Saver > Start Time > Never). The sleep timer has no "never" in the menu; with ADB debugging on, `adb shell settings put secure sleep_timeout 0` sets it (Fire OS 7). An update can reset it.

## Address options

| Option | Effect |
|---|---|
| `?layout=all` | a fixed 3x2 grid of every printer instead of the adaptive layout |
| `?gauges=0` | big-number panels instead of dials |
| `?hud=0` | one job shows the camera with dials, not the HUD stream |
| `?video=mjpeg` / `?video=h264` / `?video=0` | force MJPEG / H.264 (WebRTC) / still frames |
| `?vmode=mse` | H.264 over MSE instead of WebRTC (for comparison) |
| `?demo=0`...`?demo=5` | sample jobs over the real cameras (keys 0-5 switch; the bar at the top has buttons) |
| `?debug=1` | a yellow bar with the browser's screen size, scale and version |

Opened as a local file (`file://`), the page reaches go2rtc directly at `http://<host>:1984` (`?host=<ip>`); browsers then fall back to still frames for the video.
