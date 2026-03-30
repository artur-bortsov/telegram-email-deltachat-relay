#!/usr/bin/env bash
# Aardvark installer for Linux (systemd)
# Idempotent: safe to run multiple times.
# DO NOT use "set -e" at the top - prereq installers must handle errors locally.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_DIR="${AARDVARK_TARGET_DIR:-/opt/aardvark}"
SERVICE_NAME="aardvark-relay"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="${AARDVARK_SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="$TARGET_DIR/logs"

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
ask()   { read -rp "  $* [y/N] " _ans; [[ "$_ans" == [yY] || "$_ans" == [yY][eE][sS] ]]; }

# ---------------------------------------------------------------------------
if [[ "$EUID" -ne 0 ]]; then
  error "Please run this installer with sudo:"
  error "  sudo bash installers/linux/install.sh"
  exit 1
fi

echo "======================================================"
echo "  Aardvark installer - Linux"
echo "======================================================"
echo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_verify_python() {
  local py="${1:-$PYTHON_BIN}"
  command -v "$py" >/dev/null 2>&1 || return 1
  local ver maj min
  ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || return 1
  maj=$(echo "$ver" | cut -d. -f1)
  min=$(echo "$ver" | cut -d. -f2)
  [[ "$maj" -ge 3 && ( "$maj" -gt 3 || "$min" -ge 11 ) ]]
}

_manual_python_hint() {
  local distro="${1:-generic}"
  echo
  warn "Python 3.11+ could not be installed automatically."
  echo "  Please install it manually, then re-run this installer."
  echo
  case "$distro" in
    apt)
      echo "  Option 1 - built-in package (may need newer OS):"
      echo "    sudo apt-get install python3.11 python3.11-venv"
      echo "  Option 2 - deadsnakes PPA (Ubuntu):"
      echo "    sudo add-apt-repository ppa:deadsnakes/ppa"
      echo "    sudo apt-get update && sudo apt-get install python3.11 python3.11-venv"
      ;;
    dnf|yum)
      echo "    sudo dnf install python3.11  # Fedora / RHEL 8+"
      echo "  For RHEL 7: enable EPEL first: https://docs.fedoraproject.org/en-US/epel/"
      ;;
    zypper)
      echo "    sudo zypper install python311 python311-pip"
      ;;
    pacman)
      echo "    sudo pacman -S python"
      ;;
    apk)
      echo "    apk add --no-cache python3 py3-pip"
      ;;
    *)
      echo "    Download Python 3.11+ source: https://www.python.org/downloads/source/"
      ;;
  esac
  echo
}

_install_python_linux() {
  local pm=""
  command -v apt-get >/dev/null 2>&1 && pm="apt"
  [[ -z "$pm" ]] && command -v dnf     >/dev/null 2>&1 && pm="dnf"
  [[ -z "$pm" ]] && command -v yum     >/dev/null 2>&1 && pm="yum"
  [[ -z "$pm" ]] && command -v zypper  >/dev/null 2>&1 && pm="zypper"
  [[ -z "$pm" ]] && command -v pacman  >/dev/null 2>&1 && pm="pacman"
  [[ -z "$pm" ]] && command -v apk     >/dev/null 2>&1 && pm="apk"

  if [[ -z "$pm" ]]; then
    warn "No recognised package manager found."
    _manual_python_hint "generic"
    return 1
  fi

  info "Detected package manager: $pm.  Searching for latest stable Python 3.x..."

  case "$pm" in
    apt)
      apt-get update -qq 2>/dev/null || true
      # Try each version from newest to oldest (3.13 first so we get the latest).
      for v in 3.13 3.12 3.11; do
        local pv="python${v}" vv="${v//./}"
        if apt-get install -y "$pv" "${pv}-venv" 2>/dev/null; then
          if _verify_python "$pv"; then PYTHON_BIN="$pv"; info "Python $v installed."; return 0; fi
        fi
      done
      # deadsnakes PPA gives newer versions on older Ubuntu
      if command -v add-apt-repository >/dev/null 2>&1; then
        info "Trying deadsnakes PPA for Ubuntu..."
        apt-get install -y software-properties-common 2>/dev/null || true
        add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
        apt-get update -qq 2>/dev/null || true
        for v in 3.13 3.12 3.11; do
          local pv="python${v}"
          if apt-get install -y "$pv" "${pv}-venv" 2>/dev/null; then
            if _verify_python "$pv"; then PYTHON_BIN="$pv"; info "Python $v installed via deadsnakes PPA."; return 0; fi
          fi
        done
      fi
      # Last resort: whatever python3 APT ships
      if apt-get install -y python3 python3-venv 2>/dev/null; then
        if _verify_python python3; then PYTHON_BIN="python3"; info "Python 3 installed."; return 0; fi
      fi
      _manual_python_hint "apt"; return 1
      ;;
    dnf)
      for v in 3.13 3.12 3.11; do
        local pv="python${v}"
        if dnf install -y "$pv" "${pv}-pip" 2>/dev/null; then
          if _verify_python "$pv"; then PYTHON_BIN="$pv"; info "Python $v installed."; return 0; fi
        fi
      done
      if dnf install -y python3 2>/dev/null; then
        if _verify_python python3; then PYTHON_BIN="python3"; info "Python 3 installed."; return 0; fi
      fi
      _manual_python_hint "dnf"; return 1
      ;;
    yum)
      for v in 313 312 311; do
        if yum install -y "python${v}" 2>/dev/null; then
          local pv="python3.${v:1}"
          for p in "python3.${v:1}" "python${v}" python3; do
            if _verify_python "$p"; then PYTHON_BIN="$p"; info "Python installed ($p)."; return 0; fi
          done
        fi
      done
      if yum install -y python3 2>/dev/null; then
        if _verify_python python3; then PYTHON_BIN="python3"; info "Python 3 installed."; return 0; fi
      fi
      _manual_python_hint "yum"; return 1
      ;;
    zypper)
      for v in 313 312 311; do
        if zypper install -y "python${v}" "python${v}-pip" 2>/dev/null; then
          for p in "python3.${v:1}" "python${v}" python3; do
            if _verify_python "$p"; then PYTHON_BIN="$p"; info "Python installed ($p)."; return 0; fi
          done
        fi
      done
      if zypper install -y python3 2>/dev/null; then
        if _verify_python python3; then PYTHON_BIN="python3"; info "Python 3 installed."; return 0; fi
      fi
      _manual_python_hint "zypper"; return 1
      ;;
    pacman)
      # Arch always ships the latest Python as the 'python' package
      if pacman -S --noconfirm python 2>/dev/null; then
        if _verify_python python; then PYTHON_BIN="python"; info "Python installed."; return 0; fi
      fi
      _manual_python_hint "pacman"; return 1
      ;;
    apk)
      if apk add --no-cache python3 py3-pip 2>/dev/null; then
        if _verify_python python3; then PYTHON_BIN="python3"; info "Python installed."; return 0; fi
      fi
      _manual_python_hint "apk"; return 1
      ;;
  esac
  return 1
}

_ensure_python() {
  if _verify_python "$PYTHON_BIN"; then
    info "Python $("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') found."
    return 0
  fi
  # Try common names before asking (newest first)
  for p in python3.13 python3.12 python3.11 python313 python312 python311 python3 python; do
    if _verify_python "$p"; then
      PYTHON_BIN="$p"
      info "Python found as $p."
      return 0
    fi
  done

  warn "Python 3.11+ not found."
  if ask "Attempt to install the latest stable Python 3.x automatically?"; then
    if _install_python_linux; then return 0; fi
    # Auto-install failed; give user a chance to install manually
    echo
    error "Automatic installation did not succeed."
    echo "  Please install Python 3.11+ using the instructions above,"
    echo "  then press Enter to check again."
    read -rp "  Press Enter when Python is installed: "
    for p in python3.13 python3.12 python3.11 python313 python312 python311 python3 python; do
      if _verify_python "$p"; then PYTHON_BIN="$p"; info "Python found as $p."; return 0; fi
    done
    error "Python 3.11+ still not found.  Cannot continue."
    exit 1
  else
    error "Python 3.11+ is required.  Install it then re-run this installer."
    exit 1
  fi
}

_ensure_rsync() {
  if command -v rsync >/dev/null 2>&1; then info "rsync found."; return 0; fi
  warn "rsync not found."
  if ask "Attempt to install rsync automatically?"; then
    local installed=false
    command -v apt-get >/dev/null 2>&1 && apt-get install -y rsync 2>/dev/null && installed=true
    command -v dnf     >/dev/null 2>&1 && [[ "$installed" == false ]] && dnf install -y rsync 2>/dev/null && installed=true
    command -v yum     >/dev/null 2>&1 && [[ "$installed" == false ]] && yum install -y rsync 2>/dev/null && installed=true
    command -v zypper  >/dev/null 2>&1 && [[ "$installed" == false ]] && zypper install -y rsync 2>/dev/null && installed=true
    command -v pacman  >/dev/null 2>&1 && [[ "$installed" == false ]] && pacman -S --noconfirm rsync 2>/dev/null && installed=true
    command -v apk     >/dev/null 2>&1 && [[ "$installed" == false ]] && apk add --no-cache rsync 2>/dev/null && installed=true
    if command -v rsync >/dev/null 2>&1; then info "rsync installed."; return 0; fi
    warn "rsync installation failed."
    echo "  Please install rsync manually (e.g. sudo apt-get install rsync),"
    echo "  then press Enter to continue."
    read -rp "  Press Enter when rsync is installed: "
    if command -v rsync >/dev/null 2>&1; then info "rsync found."; return 0; fi
    error "rsync not found.  Cannot continue."
    exit 1
  else
    error "rsync is required.  Install it then re-run this installer."
    exit 1
  fi
}

_ensure_systemd() {
  if command -v systemctl >/dev/null 2>&1; then info "systemd found."; return 0; fi
  error "systemctl not found.  This installer requires systemd."
  echo "  For non-systemd systems, see README.md for manual service setup."
  exit 1
}

_ensure_venv() {
  # Test whether the venv module is functional (python3-venv may not be installed).
  if "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    info "python3-venv available."
    return 0
  fi
  local ver
  ver=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
  local pkg="python${ver}-venv"
  warn "python3-venv module not found.  Attempting to install $pkg ..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y "$pkg" 2>/dev/null || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y "$pkg" 2>/dev/null || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y "$pkg" 2>/dev/null || true
  fi
  if "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    info "$pkg installed."
    return 0
  fi
  error "python3-venv is required but could not be installed automatically."
  echo "  Install it manually and re-run:  sudo apt-get install $pkg"
  exit 1
}

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
info "Checking prerequisites..."
_ensure_python
_ensure_venv
_ensure_rsync
_ensure_systemd


# ---------------------------------------------------------------------------
# Stop existing service before file operations (idempotent)
# On Linux files can be replaced while in use, but stopping first avoids
# mixed state where the running process loads new files piecemeal.
# ---------------------------------------------------------------------------
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  info "Stopping existing service before update..."
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  # Wait for relay.py to fully exit (up to 20 s)
  _dl=$(( SECONDS + 20 ))
  while [[ $SECONDS -lt $_dl ]]; do
    pgrep -f "app/relay.py" > /dev/null 2>&1 || break
    sleep 2
  done
  pkill -f "$TARGET_DIR/app/relay.py" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Copy application files (preserve config.toml, logs, session, state)
# ---------------------------------------------------------------------------
info "Copying application to $TARGET_DIR ..."
mkdir -p "$TARGET_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude 'config.toml' --exclude 'logs/' \
  --exclude 'deltachat_accounts/' --exclude '*.db' \
  --exclude '*.session' --exclude 'relay_state.json' --exclude 'invite_links.txt' \
  "$ROOT_DIR/" "$TARGET_DIR/"

mkdir -p "$LOG_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$LOG_DIR"

# ---------------------------------------------------------------------------
# Virtual environment and dependencies
# ---------------------------------------------------------------------------
info "Creating Python virtual environment..."
"$PYTHON_BIN" -m venv "$TARGET_DIR/.venv"
info "Installing dependencies from bundled wheels..."
if ! "$TARGET_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check --no-index --only-binary :all: \
  --find-links "$TARGET_DIR/vendor/wheels/common" \
  --find-links "$TARGET_DIR/vendor/wheels/linux-x86_64" \
  -r "$TARGET_DIR/requirements.txt"; then
  # Some bundled wheels may be missing for this Python version (e.g. pyaes for
  # Python 3.13).  Fall back to online PyPI, still preferring the local bundle.
  warn "Offline install incomplete.  Falling back to online install for missing packages..."
  if ! "$TARGET_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check \
    --find-links "$TARGET_DIR/vendor/wheels/common" \
    --find-links "$TARGET_DIR/vendor/wheels/linux-x86_64" \
    -r "$TARGET_DIR/requirements.txt"; then
    error "Dependency installation failed (both offline and online)."
    echo "  Check your internet connection, then retry or install manually:"
    echo "    $TARGET_DIR/.venv/bin/pip install -r $TARGET_DIR/requirements.txt"
    exit 1
  fi
  info "Dependencies installed (some downloaded from PyPI)."
fi

# ---------------------------------------------------------------------------
# Configuration check / wizard
# ---------------------------------------------------------------------------
# Use arrays so paths with spaces are handled correctly.
VALIDATOR=("$TARGET_DIR/.venv/bin/python" "$TARGET_DIR/tools/validate_config.py")
WIZARD=("$TARGET_DIR/.venv/bin/python" "$TARGET_DIR/tools/config_wizard.py" "--output" "$TARGET_DIR/config.toml" "--install-dir" "$TARGET_DIR")

if ! "${VALIDATOR[@]}" "$TARGET_DIR/config.toml" --require-complete >/dev/null 2>&1; then
  echo
  warn "Configuration is missing or incomplete.  Starting setup wizard..."
  echo
  "${WIZARD[@]}"
fi

if ! "${VALIDATOR[@]}" "$TARGET_DIR/config.toml" --require-complete; then
  error "Configuration is still incomplete.  Re-run the installer to try again."
  exit 1
fi

# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------
chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET_DIR"
chmod 600 "$TARGET_DIR/config.toml"

# ---------------------------------------------------------------------------
# Firewall (best-effort)
# ---------------------------------------------------------------------------
if command -v ufw >/dev/null 2>&1; then
  UFW_STATUS=$(ufw status 2>/dev/null | head -1)
  if [[ "$UFW_STATUS" == *"active"* ]]; then
    info "UFW is active.  Aardvark uses outbound TCP on 443 (Telegram), 993 (IMAP), 465/587 (SMTP)."
    info "UFW default-allow-outgoing covers these automatically.  No changes needed."
  fi
elif command -v firewall-cmd >/dev/null 2>&1; then
  info "firewalld detected.  Aardvark uses only outbound connections - no inbound rules needed."
fi

# ---------------------------------------------------------------------------
# systemd service
# ---------------------------------------------------------------------------
info "Installing systemd service..."
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Aardvark Telegram Email Delta Chat Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$TARGET_DIR
ExecStart=$TARGET_DIR/.venv/bin/python $TARGET_DIR/app/relay.py \
    --config $TARGET_DIR/config.toml \
    --log-level INFO \
    --log-file $LOG_DIR/relay.log
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
# Enable service but do NOT start it yet.
# _post_install_check will run --login if the session is missing,
# then start the service.  This ensures the service only starts AFTER
# the user has authenticated interactively in this terminal.
systemctl enable "$SERVICE_NAME"


# ---------------------------------------------------------------------------
# Post-install health check
# ---------------------------------------------------------------------------

_run_login() {
  echo
  echo "================================================================"
  echo "  TELEGRAM AUTHENTICATION"
  echo "  Telegram will send/has sent an SMS code to your phone."
  echo "  >>> TYPE THE CODE IN THIS WINDOW, AT THE PROMPT BELOW <<<"
  echo "  If 2FA / Cloud Password is enabled, a second prompt appears"
  echo "  right after the SMS code - type your password there too."
  echo "================================================================"
  echo
  # cd to install dir so the .session file is always created there
  local _prev="$PWD"; cd "$TARGET_DIR"
  sudo -u "$SERVICE_USER" "$TARGET_DIR/.venv/bin/python" \
    "$TARGET_DIR/app/relay.py" \
    --login --config "$TARGET_DIR/config.toml"
  local _rc=$?; cd "$_prev"; return $_rc
}

_post_install_check() {
  # 1. Check for Telegram session file
  local session_found=false
  for f in "$TARGET_DIR"/*.session; do
    [[ -f "$f" ]] && session_found=true && break
  done

  if [[ "$session_found" == false ]]; then
    echo
    warn "No Telegram session file found."
    echo "  The service cannot relay messages until you authenticate."
    echo "  You will be asked for:"
    echo "    1. SMS verification code Telegram sends to your phone"
    echo "    2. Your Telegram Cloud Password (only if 2FA is enabled)"
    echo
    if ask "Authenticate with Telegram now?"; then
      if _run_login; then
        info "Authentication successful. Starting service..."
        systemctl start "$SERVICE_NAME" 2>/dev/null || true
        sleep 3
      else
        warn "Authentication was not completed."
        echo "  Run it manually when ready, then restart the service:"
        echo "    sudo -u $SERVICE_USER $TARGET_DIR/.venv/bin/python \\"
        echo "      $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
        echo "    sudo systemctl restart $SERVICE_NAME"
        return
      fi
    else
      echo
      echo "  Run authentication manually before the service can work:"
      echo "    sudo -u $SERVICE_USER $TARGET_DIR/.venv/bin/python \\"
      echo "      $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
      echo "  Then restart:  sudo systemctl restart $SERVICE_NAME"
      return
    fi
  fi

  # 2. Session exists - start the service now
  info "Telegram session found.  Starting service..."
  systemctl start "$SERVICE_NAME" 2>/dev/null || true

  # 3. Wait for service to initialise and write first log lines
  info "Waiting for service connections (up to 45 s)..."
  local _w=0
  while [[ $_w -lt 45 ]]; do
    sleep 5; _w=$((_w + 5))
    [[ -f "$LOG_DIR/relay.log" ]] && log=$(tail -300 "$LOG_DIR/relay.log" 2>/dev/null || true)
    echo "$log" | grep -q "Listening for new messages\|Telegram client connected\|Relay service is running" && break
  done

  # 3. Parse logs
  local log=""
  [[ -f "$LOG_DIR/relay.log" ]] && log=$(tail -300 "$LOG_DIR/relay.log" 2>/dev/null || true)

  echo
  echo "======================================================"
  echo "  Connection status"
  echo "======================================================"

  # Telegram
  if echo "$log" | grep -q "Listening for new messages\|Telegram client connected\|Relay service is running"; then
    info "Telegram :  CONNECTED"
  elif echo "$log" | grep -q "NOT reachable.*proxy\|proxy.*NOT reachable"; then
    warn "Telegram :  PROXY UNREACHABLE"
    echo "  The proxy configured in [proxy] cannot be reached."
    echo "  Check host/port/secret in config.toml."
  elif echo "$log" | grep -q "Server closed the connection\|Connection.*failed\|ERROR.*Telegram"; then
    warn "Telegram :  CONNECTION FAILED"
    echo "  Check proxy settings and network connectivity."
    echo "  Logs: journalctl -u $SERVICE_NAME -f"
  else
    warn "Telegram :  UNKNOWN  (still starting or check logs)"
  fi

  # Delta Chat
  if echo "$log" | grep -q "DC forwarding disabled\|delta_chat.enabled = false"; then
    info "Delta Chat:  DISABLED (email-only mode)"
  elif echo "$log" | grep -q "DC chat ready\|Delta Chat I/O started\|Listening for new messages"; then
    info "Delta Chat:  CONNECTED"
  elif echo "$log" | grep -q "Failed to start Delta Chat\|IMAP failed\|JsonRpcError"; then
    warn "Delta Chat:  CONNECTION FAILED"
    echo "  Check email credentials in [delta_chat] in config.toml."
    echo "  If your network needs a proxy, add a [dc_proxy] section"
    echo "  with type=socks5 (NOT mtproto)."
  else
    warn "Delta Chat:  UNKNOWN  (still starting or check logs)"
  fi

  # Email relay
  if echo "$log" | grep -q "E-mail relay enabled"; then
    info "Email relay: ENABLED"
  fi


  # --- Corrupted / expired session detection ---
  if ! echo "$log" | grep -q "Listening for new messages\|Telegram client connected\|Relay service is running"; then
    local _sessions=("$TARGET_DIR"/*.session)
    if [[ "${#_sessions[@]}" -gt 0 && -f "${_sessions[0]}" ]]; then
      echo
      warn "Telegram did not connect even though a session file exists."
      echo "  The session is likely expired or corrupted."
      echo "  Telegram has probably already sent a verification code to your phone."
      echo
      if ask "Fix: stop service, remove old session, and re-authenticate now?"; then
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        sleep 4
        rm -f "$TARGET_DIR"/*.session
        info "Old session removed."
        if _run_login; then
          info "Authentication successful. Restarting service..."
          systemctl start "$SERVICE_NAME" 2>/dev/null || true
          sleep 5
        else
          warn "Re-authentication not completed."
          echo "  Run manually, then restart the service:"
          echo "    sudo -u $SERVICE_USER $TARGET_DIR/.venv/bin/python $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
        fi
      else
        echo "  To fix manually:"
        echo "    1. Stop the service"
        echo "    2. rm \"$TARGET_DIR\"/*.session"
        echo "    3. sudo -u $SERVICE_USER $TARGET_DIR/.venv/bin/python $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
        echo "    4. Restart the service"
      fi
    fi
  fi

  # Invite links (DC mode) - poll up to 45 s for the file to appear
  if echo "$log" | grep -q "DC chat ready\|Delta Chat I/O started"; then
    local invite="$TARGET_DIR/invite_links.txt"
    echo
    info "Waiting for Delta Chat invite links (up to 45 s)..."
    local waited=0
    while [[ ! -f "$invite" && $waited -lt 45 ]]; do
      sleep 3; waited=$((waited + 3))
    done
    if [[ -f "$invite" ]]; then
      echo
      info "Delta Chat invite links (share these to add subscribers):"
      while IFS= read -r line; do echo "    $line"; done < "$invite"
      echo
      warn "Share invite links ONLY through a secure channel (Signal, encrypted email)."
      echo "  Anyone with a link can join the broadcast channel."
    else
      warn "Invite links not yet created (DC is still setting up)."
      echo "  Check in 1-2 minutes: $invite"
    fi
  elif ! echo "$log" | grep -q "DC forwarding disabled\|delta_chat.enabled = false"; then
    echo
    warn "Invite links not available (DC not connected yet)."
    echo "  Fix the DC connection, restart the service, then check:"
    echo "    $TARGET_DIR/invite_links.txt"
  fi

  echo "======================================================"
  echo "  Full logs : $LOG_DIR/relay.log"
  echo "  Live view : journalctl -u $SERVICE_NAME -f"
  echo "======================================================"
}

_post_install_check

echo
echo "======================================================"
echo "  Aardvark installed successfully."
echo "======================================================"
echo
echo "  Installer files : $ROOT_DIR"
echo "        (your downloaded / source folder - not the service)"
echo "  Installed to     : $TARGET_DIR"
echo "        (all commands below use this path)"
echo
echo "  App dir   : $TARGET_DIR"
echo "  Config    : $TARGET_DIR/config.toml"
echo "  Logs      : $LOG_DIR/relay.log"
echo "  Inv. links: $TARGET_DIR/invite_links.txt  (after first start)"
echo "  README    : $TARGET_DIR/README.md"
echo
echo "  Service control:"
echo "    Status : sudo systemctl status $SERVICE_NAME"
echo "    Start  : sudo systemctl start  $SERVICE_NAME"
echo "    Stop   : sudo systemctl stop   $SERVICE_NAME"
echo "    Restart: sudo systemctl restart $SERVICE_NAME"
echo "    Logs   : journalctl -u $SERVICE_NAME -f"
echo "             or: tail -f $LOG_DIR/relay.log"
echo
echo "  Config hot-reload: edit config.toml while running;"
echo "    channel and burst changes apply automatically in ~30 s."
echo
echo "  FIRST-TIME TELEGRAM LOGIN (required before the service can run):"
echo "    sudo -u $SERVICE_USER $TARGET_DIR/.venv/bin/python \\"
echo "      $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
echo "    Telegram sends an SMS code to your phone; enter it when prompted."
echo "    If 2FA (Cloud Password) is enabled, enter it immediately after."
echo "    The session is saved and reused automatically afterwards."
echo
echo "  PROXY NOTES:"
echo "    [proxy]    in config.toml = Telegram proxy (mtproto/socks5/http)"
echo "    [dc_proxy] in config.toml = Delta Chat + email proxy (socks5/http ONLY)"
echo "    MTProto is Telegram-specific; DC/email need a separate SOCKS5 proxy."
echo "    See config_example.toml for setup examples."
echo "======================================================"
