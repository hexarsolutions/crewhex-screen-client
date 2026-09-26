#!/usr/bin/env bash
# boot/install.sh — shared first-boot layer for the Pi screen kit.
#
# Installs the platform chooser + WiFi helper and a generic kiosk that opens it.
# Picking "CrewHex" on the TV then runs the repo's existing hardened installer
# (which brings its own kiosk + OTA). Picking "Display-Hub" keeps this kiosk and
# points it at the staff.hexar.co player. Run once as root on a flashed Pi.
#
#   curl -fsSL https://raw.githubusercontent.com/hexarsolutions/crewhex-screen-client/main/boot/install.sh | sudo bash
set -euo pipefail

ETC=/etc/pi-screen
REPO=/opt/crewhex-screen-client
KIOSK_HOME="$ETC/kiosk-data"
KIOSK_USER="${SUDO_USER:-pi}"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

echo "== pi-screen boot layer: packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends chromium xserver-xorg xinit openbox network-manager python3 2>/dev/null || \
apt-get install -y --no-install-recommends chromium xserver-xorg xinit openbox network-manager python3

echo "== pi-screen boot layer: files =="
install -d "$ETC" "$KIOSK_HOME" "$REPO"
cp -r "$(cd "$(dirname "$0")/.." && pwd)/." "$REPO/"
cp "$REPO/boot/setup_server.py" "$ETC/setup_server.py"
cp "$REPO/boot/choose.sh" "$ETC/choose.sh"
chmod +x "$ETC/setup_server.py" "$ETC/choose.sh"

if [ ! -f "$ETC/kiosk.conf" ]; then
  echo "platform=choose" > "$ETC/kiosk.conf"
fi

id -u "$KIOSK_USER" >/dev/null 2>&1 || KIOSK_USER=pi

echo "== pi-screen boot layer: setup server (loopback only, :8090) =="
cat > /etc/systemd/system/pi-screen-setup.service <<EOF
[Unit]
Description=Pi screen setup server (platform chooser + wifi)
After=network.target
[Service]
ExecStart=/usr/bin/python3 $ETC/setup_server.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

echo "== pi-screen boot layer: kiosk (opens the chooser) =="
mkdir -p "$KIOSK_HOME"; chown -R "$KIOSK_USER" "$KIOSK_HOME" 2>/dev/null || true
cat > /etc/systemd/system/pi-screen-kiosk.service <<EOF
[Unit]
Description=Pi Screen Kiosk (Chromium - setup chooser)
After=lightdm.service graphical.target pi-screen-setup.service
Wants=lightdm.service
Requires=pi-screen-setup.service
[Service]
User=$KIOSK_USER
Environment=DISPLAY=:0
Environment=XAUTHORITY=$(getent passwd "$KIOSK_USER" | cut -d: -f6)/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u "$KIOSK_USER")
ExecStart=/usr/bin/chromium --user-data-dir=$KIOSK_HOME/profile --no-first-run --no-default-browser-check --ozone-platform=x11 --kiosk --noerrdialogs --disable-infobars --check-for-update-interval=31536000 --disable-session-crashed-bubble --start-fullscreen http://127.0.0.1:8090/
Restart=always
RestartSec=5
[Install]
WantedBy=graphical.target
EOF

echo "== pi-screen boot layer: enable =="
systemctl daemon-reload
systemctl enable --now pi-screen-setup.service
systemctl enable --now pi-screen-kiosk.service

echo
echo "Pi screen boot layer installed."
echo "  The TV will show the platform chooser on next boot."
echo "  CrewHex    -> runs the repo's hardened installer (own kiosk + OTA)"
echo "  Display-Hub-> kiosk serves https://staff.hexar.co/display"
echo "  logs       : journalctl -u pi-screen-setup -u pi-screen-kiosk -f"
