# Installation Guide

Step-by-step setup for a fresh deployment.

## 1. Prepare your Docker host

Tested on Synology DSM 7.2 with Container Manager. Should work on any Linux host with Docker 24+ and Docker Compose v2.

On Synology:
1. Install **Container Manager** from Package Center (replaces the older "Docker" package)
2. Enable SSH (Control Panel → Terminal & SNMP)
3. SSH in and confirm `docker compose version` returns v2.x

```bash
ssh admin@your-nas
sudo -i
docker compose version
```

> **⚠️ Non-Synology Linux hosts** (Debian, Ubuntu, Proxmox, OMV, Unraid, etc.):
> The ONVIF wrapper needs each printer's virtual MAC to live on its own
> macvlan network interface. Synology DSM auto-creates these; other distros
> don't. Before completing this install, follow [LINUX_NETWORKING.md](LINUX_NETWORKING.md)
> to manually create the macvlan interfaces — otherwise the wrapper container
> will fail with `Failed to find IP address for MAC address`.

## 2. Enable LAN Mode Liveview on each printer

This is what exposes RTSPS (port 322) and MQTT (port 8883) on the printer's LAN interface.

1. Open **Bambu Handy** on your phone
2. Tap a printer → **Settings** → **General**
3. Enable **LAN Mode Liveview**
4. Repeat for each printer
5. Some firmware revisions need the printer power-cycled before the change takes effect

Verify each printer is reachable:

```bash
for ip in 192.168.x.A 192.168.x.B 192.168.x.C; do
  printf "%s:322 -> " $ip
  timeout 3 bash -c "</dev/tcp/$ip/322" 2>/dev/null && echo OPEN || echo CLOSED
done
```

All should return `OPEN`. If any are `CLOSED`, that printer's LAN Mode Liveview isn't on.

## 3. Find each printer's serial number

The serial is the `CN` value of the printer's TLS cert:

```bash
for ip in 192.168.x.A 192.168.x.B; do
  echo "=== $ip ==="
  openssl s_client -connect $ip:322 -showcerts </dev/null 2>&1 | grep "subject="
done
```

Expected output:
```
=== 192.168.x.A ===
subject=CN = XXXXXXXXXXXXXXX
=== 192.168.x.B ===
subject=CN = YYYYYYYYYYYYYYY
```

Save these — you'll need them for `printers.yaml`.

## 4. Find each printer's Access Code

In Bambu Handy: tap the printer → **Settings** → **WLAN** → **Access Code**. It's an 8-character alphanumeric string. Save these too.

## 5. Clone the repo

```bash
mkdir -p /volume1/docker/bambu-protect-overlay
cd /volume1/docker/bambu-protect-overlay
git clone https://github.com/mtnears/bambu-protect-overlay.git .
```

## 6. Configure

```bash
cp printers.example.yaml printers.yaml
cp go2rtc.example.yaml   go2rtc.yaml
cp config.example.yaml   config.yaml
```

Edit each in turn:

### `printers.yaml`

Set `site_label`, then add an entry per printer with the IP, access code, and serial you collected above.

### `go2rtc.yaml`

For each printer:
1. Add a `<name>_src:` line with the RTSPS URL (using `rtspx://` not `rtsps://`)
2. Add a public `<name>:` line referencing the ffmpeg drawtext template
3. Add a matching `drawtext=<name>:` template at the bottom

### `config.yaml`

For each printer, add an `onvif:` block with:
- A unique locally-administered MAC address
- Unique server/rtsp/snapshot port numbers (ideally in the 11000–11100 range)
- A unique UUID (`uuidgen` on Linux, or any online generator)
- Your Docker host's LAN IP as `target.hostname`
- The matching `<name>` from go2rtc as `highQuality.rtsp` and snapshot src

### `compose.yaml`

Set the `TZ` environment variable on `bambu-overlay` to your local timezone.

## 7. Bring it up

```bash
cd /volume1/docker/bambu-protect-overlay
docker compose up -d --build
```

First-run takes a minute or two to download the go2rtc and onvif-server images and build the local Python image.

## 8. Verify

```bash
docker compose ps                              # all three services Up
docker logs bambu-overlay                      # "[<name>] connected" lines
docker logs bambu-onvif | head -20             # listening on the four virtual IPs
docker logs go2rtc | tail -20                  # no errors

docker exec bambu-overlay cat /data/overlay/<name>_1.txt
docker exec bambu-overlay cat /data/overlay/<name>_2.txt
docker exec bambu-overlay cat /data/overlay/<name>_3.txt
```

Then verify the stream itself with VLC. Open Network Stream → paste:

```
rtsp://<host_ip>:8554/<name>
```

The stream should play with the overlay visible at the bottom.

## 9. Adopt in UniFi Protect

1. UniFi Protect Settings → System → enable **Discover Third-Party Cameras** (currently in beta)
2. Within ~30 seconds the cameras appear in Devices as "Onvif Cardinal" or similar
3. Adopt with any username/password (`admin`/`admin` works)
4. Click into live view — overlay should appear within a few seconds

If you get a black screen: check Stream Compatibility Mode in Protect's settings (Default has worked best in my testing). If still black, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 10. (Recommended) DHCP reservations

The virtual ONVIF cameras get their IPs via DHCP. To prevent them drifting, add reservations in your router/UniFi controller for each virtual MAC defined in `config.yaml`.

## 11. (Optional) Disable debug logging

If you have `log: level: debug` set in `go2rtc.yaml`, remove it once everything is stable to keep logs manageable. Then `docker compose restart go2rtc`.
