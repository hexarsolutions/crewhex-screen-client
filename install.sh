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
  if curl -fsSL "$BUNDLE_BASE/screen-client-$BUNDLE_VERSION.tar.gz" -o "$SRC/bundle.tar.gz"; then
    if curl -fsSL "$BUNDLE_BASE/screen-client-$BUNDLE_VERSION.tar.gz.sha256" -o "$SRC/bundle.sha256"; then
      want=$(cut -d' ' -f1 < "$SRC/bundle.sha256")
      got=$(sha256sum "$SRC/bundle.tar.gz" | cut -d' ' -f1)
      [[ "$want" == "$got" ]] || { echo "bundle checksum mismatch - refusing to install"; exit 1; }
      echo "bundle sha256 ok"
    fi
    tar -xzf "$SRC/bundle.tar.gz" -C "$SRC"
  else
    echo "no bundle at $BUNDLE_BASE - falling back to GitHub (needs access)"
    curl -fsSL "$REPO_URL/archive/refs/heads/main.tar.gz" | tar -xz -C "$SRC" --strip-components=1
  fi
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
KIOSK_HOME=$(getent passwd "$KIOSK_USER" | cut -d: -f6)
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
ExecStart=/usr/bin/chromium --ozone-platform=x11 --kiosk --noerrdialogs --disable-infobars --disable-features=Translate --check-for-update-interval=31536000 --autoplay-policy=no-user-gesture-required --disable-session-crashed-bubble --start-fullscreen http://127.0.0.1:8080/
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
