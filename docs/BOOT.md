# Pi screen boot layer (this repo, `boot/`)

One Raspberry Pi image (or flashed SD card) that a staff member or client can
plug into any old monitor/TV on site and have a working office display for
**either** CrewHex or Hexar's Display-Hub (staff.hexar.co).

## The flow on site

1. Plug the Pi into the TV's HDMI and power it (powering the Pi from the TV's
   USB port means one remote button turns the whole rig off/on).
2. First boot shows the **platform chooser**: *CrewHex screen* or
   *Hexar Display-Hub screen*. Sticky — power cycles never re-ask.
3. **WiFi**: Ethernet skips it. Wireless → the chooser screen has a *Join site
   WiFi* link (nmcli-backed; any USB keyboard works for the 2 minutes it takes).
   Hotel/office captive-portal pages render inside the kiosk browser.
4. The kiosk loads the chosen player and shows its 6-character pairing code.
5. Head office claims the code on the platform's Screens page, names the
   screen, and content starts within ~15s.
6. TV off/on → boots straight back to content. No re-pairing.

## Platform routing

| Platform | Player URL | Pairing surface | What runs |
|----------|-----------|-----------------|-----------|
| CrewHex | `https://app.crewhex.com/player/` | CrewHex Screens page | this repo's existing hardened `install.sh` (own kiosk, WiFi onboarding, OTA updates) |
| Display-Hub | `https://staff.hexar.co/display` | staff.hexar.co Screens page | this repo's `boot/` kiosk — a plain Chromium kiosk to the web player |

The Display-Hub player is fully web-based (no agent on the device), so the
Display-Hub path needs no OTA client — the browser just keeps the page fresh.

## What's in `boot/`

- `install.sh` — idempotent first-boot installer: packages, the loopback-only
  setup server, the generic kiosk unit, and sticky `/etc/pi-screen/kiosk.conf`.
- `setup_server.py` — Python stdlib HTTP server on `127.0.0.1:8090` (loopback
  only): serves the chooser, the WiFi join page, and redirects to the chosen
  player. Reads `PI_SCREEN_ETC`/`PI_SCREEN_PORT` env (used by tests).
- `choose.sh` — applies the choice: `crewhex` disables the generic kiosk and
  runs `install.sh` (the hardened CrewHex installer); `displayhub` just writes
  the config (the generic kiosk keeps serving and redirects to the player).

## Building a card (manual for v1)

1. Raspberry Pi Imager → Raspberry Pi OS **64-bit Bookworm**, with user, SSH,
   timezone set (add site WiFi in Imager if known).
2. Flash, boot once, then on the Pi:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/hexarsolutions/crewhex-screen-client/main/boot/install.sh | sudo bash
   sudo reboot
   ```
3. The TV shows the chooser. Pick a platform, join WiFi, pair in Screens.
4. To bake a batch of identical cards: flash one fully-configured card, then
   `dd` it to the rest (same hardware) — every card is generic (no secrets),
   so nothing to scrub before copying.

## Device guidance

- **Standard kit device: Raspberry Pi 4 (2GB)** or newer (Pi Zero 2W for light
  duty). This is the only device class that boots unattended into a kiosk.
- **Amazon Fire Stick / smart-TV browsers: configure-only fallback.** They
  cannot be flashed (locked bootloader, Fire OS) and do not kiosk reliably;
  they boot to the vendor launcher and need a remote. If used anyway: Fully
  Kiosk Browser pointed at the player URL, screensaver/sleep off, overscan
  adjusted.

## Status

v1 scaffold — built and unit-tested (setup server behaviour), not yet field-
tested on hardware. Image-baking tooling and an end-to-end on-Pi test pass are
the next step before cards are handed out.
