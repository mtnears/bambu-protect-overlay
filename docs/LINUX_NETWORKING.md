# Linux Host Networking (non-Synology)

The `rtsp-to-onvif` container needs each virtual ONVIF camera to live on its
own network interface with the MAC address declared in `config.yaml`. On
**Synology DSM**, this happens automatically — DSM's network stack handles
macvlan creation for the wrapper. On most other Linux hosts (vanilla Debian,
Ubuntu, Proxmox, OMV, Unraid, etc.) you'll see this error in the wrapper's
logs:

```
Failed to find IP address for MAC address <yourMAC>
```

That's the wrapper telling you it can't find an interface bound to the MAC
address you defined. The fix is to **create the macvlan interface manually**,
once per printer, before the wrapper container starts.

> Big thanks to [@jeronthenet23](https://github.com/jeronthenet23) for
> identifying this gap on Debian 12 / OMV 7 and contributing the initial
> writeup that this guide was built from.

## Quick Setup (one printer)

1. **Find your physical NIC name** — the interface that has your LAN IP:
   ```bash
   ip -br link show
   ```
   Look for the active interface (`eth0`, `ens18`, `eno1`, `enp4s0`, etc.).

2. **Create a macvlan interface** for the printer's MAC. Replace `eno1` with
   your NIC, the MAC with the one from your `config.yaml`, and the IP with
   one outside your DHCP range (or use DHCP — see below):
   ```bash
   sudo ip link add onvif0 link eno1 address <MAC-FROM-CONFIG> type macvlan mode bridge
   sudo ip addr add 192.168.1.211/24 dev onvif0
   sudo ip link set onvif0 up
   ```

3. **Verify** the interface is up and has the right MAC:
   ```bash
   ip -br addr show onvif0
   ```

4. **Start the wrapper:**
   ```bash
   docker compose up -d rtsp-to-onvif
   docker logs bambu-onvif
   ```

   You should see the four `SERVER:` and eight `PROXY:` lines, no errors.

   If the wrapper container was already running (and crashing) before you
   created the macvlan interface, it may need a kick to pick up the new
   interface:
   ```bash
   docker restart bambu-onvif
   ```

## Multi-Printer Setup

Each printer needs its own macvlan interface. Repeat the `ip link add` /
`ip addr add` / `ip link set up` block for each MAC in `config.yaml`,
incrementing the interface name (`onvif0`, `onvif1`, `onvif2`, ...) and
the static IP each time:

```bash
# Printer 1
sudo ip link add onvif0 link eno1 address 02:00:00:00:00:01 type macvlan mode bridge
sudo ip addr add 192.168.1.211/24 dev onvif0
sudo ip link set onvif0 up

# Printer 2
sudo ip link add onvif1 link eno1 address 02:00:00:00:00:02 type macvlan mode bridge
sudo ip addr add 192.168.1.212/24 dev onvif1
sudo ip link set onvif1 up

# ... and so on
```

## Persistence Across Reboots

`ip link add` is non-persistent — interfaces disappear at reboot. Two options
to make this survive a host restart:

### Option A: systemd service (recommended)

Create `/etc/systemd/system/onvif-macvlan.service`:

```ini
[Unit]
Description=Create macvlan interfaces for ONVIF wrapper
After=network-online.target
Wants=network-online.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/ip link add onvif0 link eno1 address 02:00:00:00:00:01 type macvlan mode bridge
ExecStart=/usr/sbin/ip addr add 192.168.1.211/24 dev onvif0
ExecStart=/usr/sbin/ip link set onvif0 up

# Repeat the three ExecStart lines for additional printers, incrementing
# the interface name and IP each time.

ExecStop=/usr/sbin/ip link delete onvif0

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now onvif-macvlan.service
sudo systemctl status onvif-macvlan.service
```

### Option B: `/etc/network/interfaces` (Debian-family)

Add to `/etc/network/interfaces`:

```
auto onvif0
iface onvif0 inet static
    address 192.168.1.211/24
    pre-up ip link add onvif0 link eno1 address 02:00:00:00:00:01 type macvlan mode bridge
    post-down ip link delete onvif0
```

Then `sudo ifup onvif0`.

## Static IP vs DHCP for the macvlan

The example above assigns a static IP. You can instead let the macvlan get its
IP via DHCP, which is what Synology does — the printer shows up in your
router's DHCP table the same way a real device would. To use DHCP:

```bash
sudo ip link add onvif0 link eno1 address 02:00:00:00:00:01 type macvlan mode bridge
sudo ip link set onvif0 up
sudo dhclient onvif0          # request a lease
```

For systemd persistence with DHCP, add `ExecStart=/usr/sbin/dhclient onvif0`
after the `link set ... up` line.

**Reserve the MAC in your DHCP server** to keep the IP stable.

## Troubleshooting

**"RTNETLINK answers: File exists"** — the interface already exists. Either
you ran the command twice, or a leftover from a previous attempt. Delete with
`sudo ip link delete onvif0` and retry.

**Container starts but adoption fails in Protect** — the macvlan interface is
up, but Protect on a different host can't reach it. Two common causes:

1. **The host's main NIC needs `promiscuous mode` enabled** for some
   virtualized environments (Proxmox in particular):
   ```bash
   sudo ip link set eno1 promisc on
   ```

2. **Some switches block traffic when multiple MACs share one switch port**.
   Check switch port security settings, or enable "MAC flooding" tolerance.

**Wireless interfaces don't work with macvlan** — most Wi-Fi drivers refuse to
forward frames with arbitrary source MACs. You'll need a wired connection on
the host running the wrapper. (This is a kernel/driver limitation, not
specific to this project.)

**Cannot ping the macvlan IP from the host itself** — this is **expected**
behavior of macvlan. The host's main interface and the macvlan interfaces
can't talk to each other directly. Other devices on the LAN (including
UniFi Protect's NVR) can reach both fine. This won't affect the project.

## Why Synology Doesn't Need This

DSM uses a custom network stack with built-in support for what it calls
"virtual interfaces" tied to bonded NICs. When the `rtsp-to-onvif` container
declares a MAC address, DSM's networking layer auto-creates the appropriate
macvlan and assigns DHCP-supplied IPs. Other Linux distros don't do this
auto-creation — hence this guide.
