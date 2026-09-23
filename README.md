# CrewHex Screen Client

A small, dependency-free screen client for [CrewHex](https://crewhex.com)
tenant displays. Run it on a Raspberry Pi (or any small Linux box), it boots
straight into a fullscreen signage screen, shows a **pairing code**, and the
tenant links it from the CrewHex web app in under a minute. After that it
rotates the tenant's published Tenant Hub pages and reports a heartbeat.

**Public on purpose** — this repo contains only the screen client. All
CrewHex application code lives in private repos; this client talks to the
public API using the documented protocol in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## How it works

```
┌───────────────┐        1. shows pairing code         ┌──────────────────┐
│  Raspberry Pi │ ───────────────────────────────────▶ │  the room's TV   │
│  (this client)│                                      └──────────────────┘
│               │        2. tenant enters code in       ┌──────────────────┐
│               │ ────────────────────────────────────  │ CrewHex web app  │
│               │           (Screens section)           └──────────────────┘
│               │        3. token issued, pages flow
│               ◀────────────────────────────────────  api.crewhex.com
└───────────────┘        (content + heartbeat)
```

1. **Boot** — the client starts and, with no stored token, shows a
   fullscreen 6-digit pairing PIN.
2. **Link** — a tenant admin opens **Screens → Add screen** in the CrewHex
   tenant app and enters their **business login name** (the same identifier
   used to sign in, e.g. `hexarsolutions`) plus the 6-digit PIN shown on
   the screen. The server binds the display to that tenant and issues a
   device token.
3. **Run** — the client stores the token, polls for published pages,
   rotates them fullscreen (typography and padding scale with the TV so a
   big screen stays readable), handles after-hours mode, and heartbeats so
   the tenant can see the screen is online.

## Install (Raspberry Pi)

### For tenants — the easy way (no experience needed)

*You need: a Raspberry Pi 3/4/5, a power supply, a microSD card (8GB+), a TV
or monitor with HDMI, and internet (Wi-Fi or ethernet cable).*

1. **Put the CrewHex software on a memory card** — on any computer,
   install the free "Raspberry Pi Imager" from
   [raspberrypi.com/software](https://www.raspberrypi.com/software/).
   Insert the microSD card, open the Imager, choose
   *Raspberry Pi OS with desktop* (not Lite; the kiosk uses the existing
   LightDM/Xwayland desktop session), choose your SD card, then click
   **Next → Edit Settings** and set:
   - hostname: `crewhex-screen`
   - enable SSH (optional, for support)
   - your Wi-Fi network name + password (if not using a cable)
2. **Write the card** (takes ~10 minutes), put it in the Pi, connect the
   Pi to the TV with HDMI, and plug in the power. It boots by itself in
   about a minute.
3. **One command** — on the Pi's first boot, open a terminal
   (black icon top-left) and type this single line:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/hexarsolutions/crewhex-screen-client/main/install.sh | sudo bash
   ```

   The installer installs the client and systemd services and starts them.
   It reuses the Pi's existing desktop session, so you normally don't need
   a separate X setup or a reboot; allow a few minutes for package installs.
4. **Link it to your business** — the TV now shows a 6-digit PIN.
   On any computer, sign in to CrewHex, open **Screens → Add screen**,
   type your business login name (the same one you use to sign in,
   e.g. `hexarsolutions`) plus the 6-digit PIN, and give the screen a
   name like `Workshop TV`. Done — the screen starts showing your
   published pages straight away.

*If anything goes wrong:* the screen tells you what it's doing. If it says
"Reconnecting…", check the network cable/Wi-Fi. Full troubleshooting guide
is below. If you're stuck, contact CrewHex support — the screen shows its
status at all times.

### Command-line install (for the tech-minded)

On a fresh Raspberry Pi OS (Bookworm/Bullseye) with internet access:

```bash
curl -fsSL https://raw.githubusercontent.com/hexarsolutions/crewhex-screen-client/main/install.sh | sudo bash
```

Or from a downloaded release package:

```bash
tar -xzf crewhex-screen-client-*.tar.gz
cd crewhex-screen-client-*/
sudo bash install.sh
```

Environment overrides when installing:

| Variable | Default | Purpose |
|---|---|---|
| `CREWHEX_API_BASE` | `https://api.crewhex.com` | API the screen talks to |
| `CREWHEX_DISPLAY_NAME` | `Screen` | friendly name pre-fill |

What the installer sets up:

* `/opt/crewhex-screen/` — the client
* `/etc/crewhex-screen/config.json` — `api_base`, `display_name`, `port`
* `/var/lib/crewhex-screen/device.json` — the device token (0600)
* `crewhex-screen.service` — the client (systemd, restarts on failure,
  hardened sandbox: OTA updates stage under `/var/lib/crewhex-screen`,
  never `/tmp`)
* `crewhex-kiosk.service` — Chromium in kiosk mode, running **inside the
  Raspberry Pi's own desktop session** (LightDM autologin on display `:0`)
  pointed at the client — the installer never starts a second X server
* A dedicated `crewhex-screen` system user (video/input groups)

Logs:

```bash
journalctl -u crewhex-screen -u crewhex-kiosk -f
```

## Updating the screen (OTA)

From the tenant app (**Screens → Client update → Push to screen**) the
server queues a new client version. A healthy Pi checks about every 20s,
downloads the OTA bundle, applies it, and restarts the client service (the
kiosk stays in the existing desktop session). Downloads have a timeout; on
failure the client logs the error and retries on a later cycle. Confirm the
new version and a fresh heartbeat in Screens before considering the update
done. GitHub tag releases include both the tenant install archive and the
root-layout `screen-client-vX.Y.Z.tar.gz` used by this OTA endpoint.

To re-install a specific GitHub release manually (for example, when OTA is
not completing), download that release package and run its installer. This
preserves the paired device token under `/var/lib/crewhex-screen/device.json`:

```bash
VERSION=v1.1.7
curl -fL "https://github.com/hexarsolutions/crewhex-screen-client/releases/download/${VERSION}/crewhex-screen-client-${VERSION}.tar.gz" -o /tmp/crewhex-screen-client.tar.gz
tar -xzf /tmp/crewhex-screen-client.tar.gz -C /tmp
cd "/tmp/crewhex-screen-client-${VERSION}"
sudo bash install.sh
sudo systemctl restart crewhex-screen crewhex-kiosk
```

## Troubleshooting (plain English)

| What the screen shows | What it means | What to do |
|---|---|---|
| Big 6-digit PIN | Waiting to be linked to your business | Enter it in CrewHex → Screens → Add screen |
| PIN disappeared, pages showing | Paired and running normally | Nothing — enjoy it |
| "Reconnecting…" | Can't reach the internet | Check the ethernet cable / Wi-Fi password; it recovers on its own |
| "Outside operating hours" | Your business hours settings | Change hours in CrewHex (Screens settings) |
| Black screen / no signal | TV input or power | Check HDMI is in the right input, power LED on the Pi is lit |
| PIN stuck for a long time | It has never been linked | The PIN refreshes every ~15 minutes; pair from CrewHex |
| Text too small / too large on the TV | Sizing is viewport-scaled | Update to v1.1.4+ (Screens → Client update) |
| Pages stop after a reconnect | Older clients could clear the slide list but not rebuild it on reconnect | Update to v1.1.5+ and confirm applied in Screens |

To get the PIN back on a screen that was already linked (e.g. moving it to
a different site), on the Pi's terminal run:

```bash
sudo systemctl stop crewhex-screen
sudo rm /var/lib/crewhex-screen/device.json
sudo systemctl start crewhex-screen
```

## Uninstall / re-pair

```bash
sudo systemctl disable --now crewhex-screen crewhex-kiosk
sudo rm -rf /opt/crewhex-screen /etc/crewhex-screen /var/lib/crewhex-screen
sudo rm /etc/systemd/system/crewhex-{screen,kiosk}.service
```

To move a screen to another tenant, clear its token and reboot — it will
show a fresh pairing code:

```bash
sudo systemctl stop crewhex-screen
sudo rm /var/lib/crewhex-screen/device.json
sudo systemctl start crewhex-screen
```

## Development

```bash
python3 client/screen_client.py            # serves http://127.0.0.1:8080
# open the URL in a browser - pairing screen (no server needed to see it)
```

The client uses **stdlib only** (no pip install). Pairing-mode works
against the screen alone; content mode lights up once the server side
(pairing + token-authed content) is deployed — the exact JSON contract is
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

Page layout: pages are built in the tenant app's **page builder** (a 16:9
canvas editor with drag/resize, templates and undo). From screen client v1.1.5,
blocks may carry `x/y/w/h` percentages for canvas positioning; blocks without
geometry keep the classic stacked layout. Older clients may not match the
canvas geometry, so update the screen before using positioned layouts. Client
v1.1.6 also grants the narrowly scoped write access required for OTA file swaps
and keeps content polling alive when an update attempt fails. v1.1.7 adds a
dedicated after-hours **Screen saver (7)**: it is excluded from the normal
1–6 rotation and can show the tenant's company logo, uploaded image, and text.
Choose it under **Tenant Hub → After-hours display**; edit and publish the
Screen saver (7) page like any other canvas page. The Pi displays the last
published saver when outside operating hours (or when paused), if selected.

## Status

- [x] Screen client (pairing, content rotation, after-hours, heartbeat)
- [x] OTA updates from the Tenant Hub (staged safely, timeout-guarded, worker survives failures)
- [x] Viewport-scaled kiosk typography (readable on large TVs)
- [x] Installer + systemd/kiosk setup for Raspberry Pi OS (reuses the LightDM desktop session)
- [x] CI packaging (release tarballs per tag)
- [x] Server: pairing endpoint, token-authed content + heartbeat *(CrewHex app)*
- [x] Server: **Screens** admin page in the tenant app *(CrewHex app)*
