#!/usr/bin/env bash
# Remove the wardrive uploader service. Keeps config + archives unless --purge.
set -euo pipefail

SERVICE="/etc/systemd/system/wardrive-uploader.service"
CONFIG_DIR="/etc/wardrive-uploader"
STATE_DIR="/var/lib/wardrive-uploader"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./uninstall.sh [--purge]" >&2
  exit 1
fi

systemctl stop wardrive-uploader 2>/dev/null || true
systemctl disable wardrive-uploader 2>/dev/null || true
rm -f "$SERVICE"
systemctl daemon-reload
echo "==> service removed"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
  echo "==> purged config and local archives"
else
  echo "==> kept $CONFIG_DIR and $STATE_DIR (use --purge to remove)"
fi
