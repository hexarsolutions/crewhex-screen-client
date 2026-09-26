#!/usr/bin/env bash
# choose.sh — apply a platform choice made on the first-boot chooser.
#   choose crewhex    -> run the existing hardened CrewHex installer
#   choose displayhub -> point the kiosk at the staff.hexar.co Display-Hub player
set -euo pipefail

ETC=/etc/pi-screen
REPO=/opt/crewhex-screen-client

case "${1:-}" in
  crewhex)
    # Stop our generic kiosk first: the CrewHex installer brings its own.
    systemctl disable --now pi-screen-kiosk.service 2>/dev/null || true
    bash "$REPO/install.sh"
    sed -i 's|^platform=.*|platform=crewhex|' "$ETC/kiosk.conf"
    ;;
  displayhub)
    sed -i 's|^platform=.*|platform=displayhub|' "$ETC/kiosk.conf"
    echo "platform=displayhub" > "$ETC/kiosk.conf"
    ;;
  *) echo "usage: choose.sh crewhex|displayhub"; exit 1 ;;
esac
