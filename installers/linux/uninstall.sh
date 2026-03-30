#!/usr/bin/env bash
# Aardvark uninstaller for Linux (systemd)
# Idempotent: safe to run multiple times.
TARGET_DIR="${AARDVARK_TARGET_DIR:-/opt/aardvark}"
SERVICE_NAME="aardvark-relay"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "$EUID" -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash installers/linux/uninstall.sh"
  exit 1
fi

echo "======================================================"
echo "  Aardvark uninstaller - Linux"
echo "======================================================"

# Stop and disable service
systemctl stop  "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "$SERVICE_FILE"
systemctl daemon-reload 2>/dev/null || true

# Offer to keep config and logs
KEEP_DATA=false
read -rp "  Keep config.toml and logs? [y/N] " _ans
if [[ "${_ans,,}" == "y" || "${_ans,,}" == "yes" ]]; then
  KEEP_DATA=true
  echo "  Backing up config and logs to /tmp/aardvark-backup/ ..."
  mkdir -p /tmp/aardvark-backup
  [[ -f "$TARGET_DIR/config.toml" ]]  && cp "$TARGET_DIR/config.toml" /tmp/aardvark-backup/ && echo "  Saved config.toml"
  [[ -d "$TARGET_DIR/logs" ]]         && cp -r "$TARGET_DIR/logs"     /tmp/aardvark-backup/ && echo "  Saved logs/"
fi

rm -rf "$TARGET_DIR"
echo
echo "  Aardvark removed."
if $KEEP_DATA; then
  echo "  Config and logs are in: /tmp/aardvark-backup/"
fi
