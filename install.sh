#!/usr/bin/env bash
# One-shot, idempotent installer for the wardrive upload appliance.
# Usage:  sudo ./install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
VENV_PY="$VENV/bin/python"
CONFIG_DIR="/etc/wardrive-uploader"
CONFIG="$CONFIG_DIR/config.ini"
SERVICE="/etc/systemd/system/wardrive-uploader.service"
STATE_DIR="/var/lib/wardrive-uploader"

c_green='\033[0;32m'; c_yellow='\033[1;33m'; c_red='\033[0;31m'; c_off='\033[0m'
info() { echo -e "${c_green}==>${c_off} $*"; }
warn() { echo -e "${c_yellow}!! ${c_off} $*"; }
err()  { echo -e "${c_red}xx ${c_off} $*" >&2; }

if [[ $EUID -ne 0 ]]; then
  err "Please run with sudo:  sudo ./install.sh"
  exit 1
fi

# The user who invoked sudo - used for the i2c group + config ownership hints.
RUN_USER="${SUDO_USER:-root}"

# --- 1. sanity-check platform (warn only) -----------------------------------
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  info "OS: ${PRETTY_NAME:-unknown}  ($(uname -m))"
  case "${VERSION_CODENAME:-}" in
    bookworm|bullseye) : ;;
    *) warn "Untested OS '${VERSION_CODENAME:-?}'. Continuing anyway." ;;
  esac
fi

# --- 2. apt dependencies ----------------------------------------------------
info "Installing apt packages..."
apt-get update -qq
apt-get install -y python3-venv python3-pip i2c-tools exfatprogs >/dev/null
# exfat-fuse only exists on older releases; install if available, ignore if not.
apt-get install -y exfat-fuse >/dev/null 2>&1 || true

# --- 3. enable I2C ----------------------------------------------------------
info "Enabling I2C..."
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || warn "raspi-config do_i2c failed; check manually"
fi
# Belt-and-suspenders: ensure the overlay + module are present.
BOOT_CFG="/boot/firmware/config.txt"
[[ -f "$BOOT_CFG" ]] || BOOT_CFG="/boot/config.txt"
if [[ -f "$BOOT_CFG" ]] && ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CFG"; then
  echo "dtparam=i2c_arm=on" >> "$BOOT_CFG"
  warn "Enabled i2c in $BOOT_CFG - a REBOOT is required for the display."
  NEED_REBOOT=1
fi
grep -q "^i2c-dev" /etc/modules 2>/dev/null || echo "i2c-dev" >> /etc/modules
if [[ "$RUN_USER" != "root" ]]; then
  usermod -aG i2c "$RUN_USER" 2>/dev/null || true
fi

# --- 4. python venv + deps --------------------------------------------------
info "Creating virtualenv at $VENV ..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

# --- 5. verify scrollphat imports (compatibility check) ---------------------
info "Checking display library..."
if "$VENV_PY" -c "import scrollphat" 2>/dev/null; then
  info "scrollphat OK"
else
  warn "scrollphat did not import. The smbus2 fallback driver will be used"
  warn "(set [display] backend if needed). Status display still works."
fi

# --- 6. config --------------------------------------------------------------
mkdir -p "$CONFIG_DIR" "$STATE_DIR"
if [[ -f "$CONFIG" ]]; then
  info "Keeping existing $CONFIG"
else
  install -m 600 "$APP_DIR/config.example.ini" "$CONFIG"
  info "Created $CONFIG (chmod 600)"
  echo
  warn "Add your API keys now (or edit $CONFIG later)."
  read -r -p "    WiGLE API name  (blank to skip): " WN || true
  read -r -p "    WiGLE API token (blank to skip): " WT || true
  read -r -p "    wdgowars API key (blank to skip): " WK || true
  [[ -n "${WN:-}" ]] && sed -i "s|^api_name =.*|api_name = ${WN}|" "$CONFIG"
  [[ -n "${WT:-}" ]] && sed -i "s|^api_token =.*|api_token = ${WT}|" "$CONFIG"
  [[ -n "${WK:-}" ]] && sed -i "s|^api_key =.*|api_key = ${WK}|" "$CONFIG"
  chmod 600 "$CONFIG"
fi

# --- 7. systemd service -----------------------------------------------------
info "Installing systemd service..."
sed -e "s|__VENV_PYTHON__|$VENV_PY|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/systemd/wardrive-uploader.service.tmpl" > "$SERVICE"
systemctl daemon-reload
systemctl enable wardrive-uploader >/dev/null
systemctl restart wardrive-uploader

# --- 8. done ----------------------------------------------------------------
echo
info "Installed. Service status:"
systemctl --no-pager --lines=0 status wardrive-uploader || true
echo
info "Follow logs with:   journalctl -u wardrive-uploader -f"
info "Test the display:   sudo $VENV_PY $APP_DIR/src/main.py --test-display"
if [[ "${NEED_REBOOT:-0}" == "1" ]]; then
  echo
  warn "I2C was just enabled - reboot before the Scroll pHAT will light up:  sudo reboot"
fi
