#!/usr/bin/env bash
# Aardvark uninstaller for macOS (launchd)
TARGET_DIR="${AARDVARK_TARGET_DIR:-$HOME/Library/Application Support/Aardvark}"
PLIST_FILE="$HOME/Library/LaunchAgents/com.aardvark.relay.plist"
LABEL="com.aardvark.relay"
BACKUP_DIR="$HOME/Desktop/aardvark-backup"

echo "======================================================"
echo "  Aardvark uninstaller - macOS"
echo "======================================================"

launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true
rm -f "$PLIST_FILE"

read -rp "  Keep config.toml and logs? [y/N] " _ans
if [[ "$_ans" == [yY] || "$_ans" == [yY][eE][sS] ]]; then
  mkdir -p "$BACKUP_DIR"
  [[ -f "$TARGET_DIR/config.toml" ]] && cp "$TARGET_DIR/config.toml" "$BACKUP_DIR/" && echo "  Saved config.toml to $BACKUP_DIR"
  [[ -d "$TARGET_DIR/logs" ]]        && cp -r "$TARGET_DIR/logs"     "$BACKUP_DIR/" && echo "  Saved logs/ to $BACKUP_DIR"
fi

rm -rf "$TARGET_DIR"
echo
echo "  Aardvark removed from macOS."
