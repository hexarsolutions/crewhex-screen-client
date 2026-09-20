#!/usr/bin/env bash
# CrewHex Screen Client - Raspberry Pi installer.
# Run on a fresh Raspberry Pi OS (Bookworm or Bullseye) with internet access:
#   curl -fsSL https://raw.githubusercontent.com/hexarsolutions/crewhex-screen-client/main/install.sh | sudo bash
# Or from a downloaded copy:  sudo bash install.sh
set -euo pipefail

REPO_URL="https://github.com/hexarsolutions/crewhex-screen-client"
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
  curl -fsSL "$REPO_URL/archive/refs/heads/main.tar.gz" | tar -xz -C "$SRC" --strip-components=1
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
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
chmod 750 "$STATE_DIR"; chmod 640 "$STATE_DIR/device.json" "$CONFIG_DIR/config.json"

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
ReadWritePaths=$STATE_DIR $CONFIG_DIR

[Install]
WantedBy=multi-user.target
EOF

echo "== kiosk (chromium, fullscreen) =="
# Lightweight X session: openbox autostarts chromium in kiosk mode.
cat > /etc/systemd/system/crewhex-kiosk.service <<EOF
[Unit]
Description=CrewHex Screen Kiosk (Chromium)
After=crewhex-screen.service
Requires=crewhex-screen.service

[Service]
User=$SERVICE_USER
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u $SERVICE_USER)
ExecStartPre=/bin/sh -c 'test -d $XDG_RUNTIME_DIR || { mkdir -p \$XDG_RUNTIME_DIR; chown $SERVICE_USER \$XDG_RUNTIME_DIR; }'
ExecStart=/usr/bin/xinit /usr/bin/openbox-session -- :0 vt7
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

KIOSK_DIR=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
sudo -u "$SERVICE_USER" mkdir -p "$KIOSK_DIR/.config/openbox"
cat > "$KIOSK_DIR/.config/openbox/autostart" <<'EOF'
# CrewHex kiosk
xset s off -dpms &
unclutter -idle 0 &
chromium \
  --kiosk --noerrdialogs --disable-infobars --disable-features=Translate \
  --check-for-update-interval=31536000 --autoplay-policy=no-user-gesture-required \
  --disable-session-crashed-bubble --start-fullscreen \
  http://127.0.0.1:8080/ &
EOF
chown -R "$SERVICE_USER:$SERVICE_USER" "$KIOSK_DIR/.config"

# Permit the service user to use the X server on vt7
cat > /etc/X11/Xwrapper.config <<'EOF'
allowed_users=anybody
needs_root_rights=yes
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
