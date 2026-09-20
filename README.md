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
   fullscreen pairing code (`XXX-XXX`).
2. **Link** — a tenant admin opens **Screens** in the CrewHex tenant app
   and enters the code. The server binds the display to that tenant and
   issues a device token.
3. **Run** — the client stores the token, polls for published pages,
   rotates them fullscreen, handles after-hours mode, and heartbeats so
   the tenant can see the screen is online.

## Install (Raspberry Pi)

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
* `crewhex-screen.service` — the client (systemd, restarts on failure)
* `crewhex-kiosk.service` — X + Chromium fullscreen pointing at the client
* A dedicated `crewhex-screen` system user (video/input groups)

Logs:

```bash
journalctl -u crewhex-screen -u crewhex-kiosk -f
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

## Status

- [x] Screen client (pairing, content rotation, after-hours, heartbeat)
- [x] Installer + systemd/kiosk setup for Raspberry Pi OS
- [x] CI packaging (release tarballs per tag)
- [ ] Server: pairing endpoint, token-authed content + heartbeat *(CrewHex app)*
- [ ] Server: **Screens** admin page in the tenant app *(CrewHex app)*
