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

1. **Boot** — with no stored token the screen shows a 6-character pairing
   code issued by CrewHex (no Wi-Fi yet? it offers Wi-Fi setup first).
2. **Link** — a tenant admin opens **Screens → Add screen** in CrewHex and
   types the code. That's all: the code identifies the business.
3. **Run** — the client downloads the screen's playlists, schedules, Hub
   pages, images and videos to the SD card (each checked by SHA-256), plays
   them fullscreen, and keeps playing from the card if the internet drops.
   It heartbeats every minute so **Screens** shows it online, what it's
   playing, and its version.

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
4. **Link it to your business** — the TV now shows a 6-character code.
   On any computer, sign in to CrewHex, open **Screens → Add screen**,
   type the code and give the screen a name like `Workshop TV`. Done —
   it starts playing within a few seconds.

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
* `/var/lib/crewhex-screen/media/` and `manifest.json` — offline copy of the screen's content
* `/var/lib/crewhex-screen/app/<version>/` — OTA builds (`/opt` stays read-only)
* `crewhex-screen.service` — runs `launch.py`, which starts the newest good build
  and rolls back after 3 failed starts (systemd sandbox: `ProtectSystem=strict`,
  only `/var/lib/crewhex-screen` writable)
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

## Upgrading to 2.0

2.0 keeps the existing device token, so screens stay paired. Two routes:

* **Reinstall (recommended, one-time):** run the install one-liner again on the Pi.
  1.1.x OTA writes into `/opt`, which the 1.1.x service sandbox makes read-only,
  so an OTA push from 1.1.5 usually logs a "Read-only file system" error and
  changes nothing. Check with `journalctl -u crewhex-screen | grep -i read-only`.
* **OTA:** works where `/opt` happens to be writable. 2.0 then hands itself off to
  the launcher logic on first start.

From 2.0 onward, OTA updates install under `/var/lib` and are signed.

## Troubleshooting (plain English)

| What the screen shows | What it means | What to do |
|---|---|---|
| Big 6-character code | Waiting to be linked to your business | Type it in CrewHex → Screens → Add screen |
| Wi-Fi network list | Not online yet and not paired | Pick your network, or plug in a cable and choose Skip |
| Small "Offline — playing saved content" chip | Internet dropped | Nothing — it keeps playing and catches up when back |
| PIN disappeared, pages showing | Paired and running normally | Nothing — enjoy it |
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

## 2026-09-24 — server-side changes (no client update required)

- Expiring announcements: heading/text blocks can carry an optional `ends_at`
  ("Show until" in the page editor). The server strips expired blocks from both
  the legacy `/hub-device/content` payload and the v2 manifest, so screens stop
  showing expired announcements without touching the client. No tag needed.
- Signage media URLs (`/uploads/signage/...`) are now HMAC-signed
  (`?exp=…&sig=…`). Players fetch URLs from the server, so existing clients are
  unaffected; unsigned direct links now return 403.
