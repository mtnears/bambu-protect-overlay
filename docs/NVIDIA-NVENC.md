# Hardware encoding with an NVIDIA GPU (NVENC)

By default go2rtc re-encodes the overlay stream with `libx264` on the CPU. On a Linux Docker host with an NVIDIA card (any NVENC-capable GeForce, Quadro or T-series), you can move that encode to the GPU.

Measured on a Dell R720 (2× Xeon E5-2690) with an NVIDIA T400, one H2S stream at 1680×1080, 30 fps, 4 Mbps:

| | CPU (ffmpeg) | GPU encoder |
|---|---|---|
| `libx264 -preset veryfast` | ~165% (1.65 cores) | – |
| `h264_nvenc -preset p4 -tune ll` | ~35–45% (decode + drawtext) | ~8% |

## What moves to the GPU (and what doesn't)

- **Moves:** the H.264 *encode* of the overlaid stream, which is by far the most expensive step.
- **Stays on the CPU:** decoding the printer's incoming stream and the `drawtext` filter, which is CPU-only in ffmpeg. That is the remaining ~0.4 core.
- **GeForce cards** have a driver limit on concurrent NVENC sessions. Each printer stream that is being watched or recorded uses one session, so with several printers plus Plex/Jellyfin transcodes you can hit it. Quadro/RTX-A and T-series cards (like the T400) have no driver limit.

## Tested with

- Debian 12, NVIDIA driver 580 with the Container Toolkit, NVIDIA T400 shared with Emby, `alexxit/go2rtc:latest-hardware`, one Bambu Lab H2S.
- Verified during a live print: the overlay renders identically (same font and layout), the stream is 1680×1080 at about 30 fps with a keyframe every second and about 4 Mbps, and UniFi Protect, the go2rtc web player and a Scrypted/HomeKit bridge all play it. The GPU encoder sits at about 8–9% for one stream.
- Measured on the earlier 2-line overlay. The 4-line overlay uses the same encoder settings.

## Requirements

- The NVIDIA driver and the NVIDIA Container Toolkit installed on the host (`nvidia-smi` works, and `docker run --rm --runtime nvidia ... nvidia-smi` works).
- The go2rtc **hardware** image, `alexxit/go2rtc:latest-hardware`. The default Alpine image's ffmpeg has no NVENC.

## Setup

1. The hardware image is Debian-based and does not ship the Droid Sans Mono font used by the overlay templates. Copy it out of the default image once:

   ```bash
   mkdir -p fonts
   docker run --rm --entrypoint cat alexxit/go2rtc /usr/share/fonts/droid/DroidSansMono.ttf > fonts/DroidSansMono.ttf
   ```

2. Create `compose.override.yaml` next to `compose.yaml`. It is gitignored, and Docker Compose merges it automatically:

   ```yaml
   services:
     go2rtc:
       image: alexxit/go2rtc:latest-hardware
       runtime: nvidia
       environment:
         - NVIDIA_VISIBLE_DEVICES=all
         - NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
       volumes:
         - ./fonts/DroidSansMono.ttf:/usr/share/fonts/droid/DroidSansMono.ttf:ro
   ```

3. In `go2rtc.yaml`, change the start of each `drawtext=<printer>:` template from

   ```
   -c:v libx264 -profile:v baseline -level:v 4.0 -preset:v veryfast
   ```

   to

   ```
   -c:v h264_nvenc -profile:v baseline -level:v 4.0 -preset:v p4 -tune:v ll -bf:v 0 -rc:v vbr
   ```

   Leave the rest of the line (`-pix_fmt`, `-g`, bitrate and `-vf drawtext...`) unchanged.

4. Run `docker compose up -d go2rtc`. To check it:

   ```bash
   nvidia-smi --query-gpu=utilization.encoder,encoder.stats.sessionCount --format=csv
   ```

   Expect one encoder session per printer stream that someone is watching (or that Protect is recording).

The GPU can be shared with Emby, Plex or Jellyfin; each overlay stream is a single NVENC session. To go back to software encoding, delete `compose.override.yaml` and revert the template line.

## Graphical HUD on NVIDIA (`overlay_style: hud`)

The HUD (see the README) can run fully on the GPU here too: decode with CUDA, composite the HUD image with `overlay_cuda`, and encode with NVENC. The camera frames arrive as NV12 in GPU memory. The HUD PNG is converted to `yuva420p` (to keep its alpha) and uploaded with `hwupload_cuda`:

```yaml
gort: 'exec:ffmpeg -hide_banner -loglevel error -hwaccel cuda -hwaccel_output_format cuda -rtsp_transport tcp -i rtsp://127.0.0.1:8554/gort_src -re -f image2 -loop 1 -framerate 10 -i /data/overlay/gort_hud.png -filter_complex [0:v]scale_cuda=format=yuv420p[cam];[1:v]format=yuva420p,hwupload_cuda[hud];[cam][hud]overlay_cuda=x=0:y=0:eof_action=repeat:repeatlast=1,hwdownload,format=yuv420p[out] -map [out] -c:v h264_nvenc -preset p4 -tune ll -bf 0 -profile:v main -g 30 -b:v 4M -maxrate 5M -bufsize 10M -an -rtsp_transport tcp -f rtsp {output}'
```

Notes:
- `-re` on the image input is required, otherwise ffmpeg reads the PNG once and never sees the per-second updates.
- `overlay_cuda` needs both inputs in GPU memory, and the HUD image must be the same size as the camera (set `video_size` in `printers.yaml`).
- `overlay_cuda` can't put `yuva420p` on `nv12` ("Can't overlay yuva420p on nv12"), so the camera is converted first with `scale_cuda=format=yuv420p`.
- The CUDA decoder pads 1680x1080 to 1696x1088. Converting on the GPU leaves green bars on the right and bottom edges, so the line downloads the composited frame (`hwdownload,format=yuv420p`) and NVENC encodes a clean 1680x1080.
- `-framerate 10` on the HUD image keeps it about 1.5 s behind the video; at 2 fps it lags about 8–10 s (see the README).
- `eof_action` and `repeatlast` are accepted by `overlay_cuda`.
- Chroma is subsampled in `yuva420p`, so thin coloured HUD edges are slightly softer than with VAAPI's RGBA overlay.
- Tested by @jeronthenet23 on a Quadro T400: no session limit, about a quarter of one CPU core per stream at 30 fps. GeForce cards have an NVENC session limit; check it if you run several printers.

