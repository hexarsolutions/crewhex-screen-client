#!/usr/bin/env bash
# CrewHex Screen Client - Raspberry Pi installer.
# Run on a fresh Raspberry Pi OS (Bookworm or Bullseye) with internet access:
#   curl -fsSL https://raw.githubusercontent.com/hexarsolutions/crewhex-screen-client/main/install.sh | sudo bash
# Or from a downloaded copy:  sudo bash install.sh
set -euo pipefail

REPO_URL="https://github.com/hexarsolutions/crewhex-screen-client"
# Rolling bundle served by CrewHex itself, so devices never need a GitHub login
BUNDLE_BASE="${CREWHEX_BUNDLE_BASE:-https://api.crewhex.com/public}"
BUNDLE_VERSION="${CREWHEX_VERSION:-latest}"
INSTALL_DIR="/opt/crewhex-screen"
CONFIG_DIR="/etc/crewhex-screen"
STATE_DIR="/var/lib/crewhex-screen"
SERVICE_USER="crewhex-screen"
API_BASE="${CREWHEX_API_BASE:-https://api.crewhex.com}"
DISPLAY_NAME="${CREWHEX_DISPLAY_NAME:-Screen}"

# The OTA signing key screens trust. Update this only when the key rotates, and
# only from client/ota_pubkey.pem in the repo.
OTA_PUBKEY_PEM="$(cat <<'PEM'
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAapLnXvBlRuvihJpulXUWuUmPCKzpta5ahpKa0FOkmq0=
-----END PUBLIC KEY-----
PEM
)"

if [[ $EUID -ne 0 ]]; then echo "run as root (sudo)"; exit 1; fi
if ! grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  echo "note: not a Raspberry Pi - continuing (any Debian works)"
fi

echo "== packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 chromium xserver-xorg xinit openbox fonts-dejavu-core \
  unclutter-startup 2>/dev/null || \
apt-get install -y --no-install-recommends \
  python3 chromium xserver-xorg xinit openbox fonts-dejavu-core

echo "== user + dirs =="
id -u "$SERVICE_USER" &>/dev/null || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
video_id=$(getent group video | cut -d: -f3)
input_id=$(getent group input | cut -d: -f3)
usermod -aG video,input "$SERVICE_USER" 2>/dev/null || true

echo "== client files =="
if [[ -f "$(cd "$(dirname "$0")" && pwd)/client/screen_client.py" ]]; then
  SRC="$(cd "$(dirname "$0")" && pwd)"          # run from a checkout/package
else
  SRC=$(mktemp -d)
  URL="$BUNDLE_BASE/screen-client-$BUNDLE_VERSION.tar.gz"
  echo "downloading $URL"
  curl -fsSL "$URL" -o "$SRC/bundle.tar.gz" || { echo "download failed - refusing to install"; exit 1; }
  # Checksum: required. A checksum that cannot be fetched is a failed install,
  # never a silent skip.
  curl -fsSL "$URL.sha256" -o "$SRC/bundle.sha256" || { echo "no checksum at $URL.sha256 - refusing to install"; exit 1; }
  want=$(cut -d' ' -f1 < "$SRC/bundle.sha256")
  got=$(sha256sum "$SRC/bundle.tar.gz" | cut -d' ' -f1)
  [[ "$want" == "$got" ]] || { echo "bundle checksum mismatch - refusing to install"; exit 1; }
  echo "bundle sha256 ok"
  # Signature: required. The public key below is the only key a screen trusts.
  curl -fsSL "$URL.sig" -o "$SRC/bundle.sig" || { echo "no signature at $URL.sig - refusing to install"; exit 1; }
  command -v openssl >/dev/null || { apt-get install -y -qq openssl || true; }
  command -v openssl >/dev/null || { echo "openssl missing - cannot verify signature"; exit 1; }
  printf '%s\n' "$OTA_PUBKEY_PEM" > "$SRC/ota_pubkey.pem"
  openssl pkeyutl -verify -pubin -inkey "$SRC/ota_pubkey.pem" -rawin \
    -in "$SRC/bundle.tar.gz" -sigfile "$SRC/bundle.sig" >/dev/null 2>&1 || {
      echo "signature check FAILED - refusing to install"; exit 1; }
  echo "bundle signature ok"
  BUNDLE_VERSION=$(tar -xzOf "$SRC/bundle.tar.gz" client/VERSION 2>/dev/null | head -1 || true)
  tar -xzf "$SRC/bundle.tar.gz" -C "$SRC"
fi
cp -r "$SRC/client/." "$INSTALL_DIR/"
cp -r "$SRC/client/kiosk" "$INSTALL_DIR/kiosk"

cat > "$CONFIG_DIR/config.json" <<EOF
{
  "api_base": "$API_BASE",
  "display_name": "$DISPLAY_NAME",
  "port": 8080
}
EOF
[[ -f "$STATE_DIR/device.json" ]] || echo '{}' > "$STATE_DIR/device.json"
# Mark a manual reinstall as applied so the paired screen updates its version in Tenant Hub.
if [[ -f "$STATE_DIR/device.json" && -f "$INSTALL_DIR/VERSION" ]]; then
  python3 - "$STATE_DIR/device.json" "$INSTALL_DIR/VERSION" <<'PY'
import json, pathlib, sys
state_path, version_path = map(pathlib.Path, sys.argv[1:])
try: state = json.loads(state_path.read_text())
except Exception: state = {}
if state.get("device_token"):
    state["updated_to"] = version_path.read_text().strip()
    state_path.write_text(json.dumps(state, indent=2))
PY
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
chmod 750 "$STATE_DIR"; chmod 600 "$STATE_DIR/device.json"; chmod 640 "$CONFIG_DIR/config.json"

echo "== systemd service =="
cat > /etc/systemd/system/crewhex-screen.service <<EOF
[Unit]
Description=CrewHex Screen Client
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
ExecStart=/usr/bin/python3 $INSTALL_DIR/screen_client.py
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
# OTA swaps only the client files under INSTALL_DIR; all other paths remain read-only.
ReadWritePaths=$STATE_DIR $CONFIG_DIR $INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF

echo "== kiosk (Chromium on the existing Raspberry Pi desktop) =="
# Raspberry Pi OS starts LightDM/Xwayland for the autologin user. Reuse that
# display instead of launching a second X server on :0.
KIOSK_USER="${SUDO_USER:-admin}"
if ! id -u "$KIOSK_USER" >/dev/null 2>&1; then
  echo "Kiosk desktop user '$KIOSK_USER' not found" >&2; exit 1
fi
KIOSK_UID=$(id -u "$KIOSK_USER")
KIOSK_HOME=$(getent passwd "$KIOSK_USER" | cut -d: -f6)   # must be set before use (set -u)
mkdir -p "$KIOSK_HOME/.config/crewhex-kiosk"; chown -R "$KIOSK_USER":"$KIOSK_USER" "$KIOSK_HOME/.config/crewhex-kiosk"
cat > /etc/systemd/system/crewhex-kiosk.service <<EOF
[Unit]
Description=CrewHex Screen Kiosk (Chromium)
After=lightdm.service graphical.target crewhex-screen.service
Wants=lightdm.service
Requires=crewhex-screen.service

[Service]
User=$KIOSK_USER
Environment=DISPLAY=:0
Environment=XAUTHORITY=$KIOSK_HOME/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/$KIOSK_UID
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$KIOSK_UID/bus
ExecStart=/usr/bin/chromium --user-data-dir=$KIOSK_HOME/.config/crewhex-kiosk --no-first-run --no-default-browser-check --ozone-platform=x11 --kiosk --noerrdialogs --disable-infobars --disable-features=Translate --check-for-update-interval=31536000 --autoplay-policy=no-user-gesture-required --disable-session-crashed-bubble --start-fullscreen http://127.0.0.1:8080/
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
EOF

echo "== enable =="
systemctl daemon-reload
systemctl enable --now crewhex-screen.service
systemctl enable --now crewhex-kiosk.service

echo
echo "CrewHex Screen installed."
echo "  config : $CONFIG_DIR/config.json   (api_base, display_name, port)"
echo "  pairing: the screen now shows a code - open Screens in the tenant app to link it."
echo "  logs   : journalctl -u crewhex-screen -u crewhex-kiosk -f"
