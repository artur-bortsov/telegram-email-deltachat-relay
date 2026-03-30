#!/usr/bin/env bash
# Aardvark installer for macOS (launchd)
# Idempotent: safe to run multiple times.
# DO NOT add "set -e" at the top - prereq functions must handle errors locally.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_DIR="${AARDVARK_TARGET_DIR:-$HOME/Library/Application Support/Aardvark}"
PLIST_FILE="$HOME/Library/LaunchAgents/com.aardvark.relay.plist"
LABEL="com.aardvark.relay"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="$TARGET_DIR/logs"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
ask()   { read -rp "  $* [y/N] " _ans; [[ "$_ans" == [yY] || "$_ans" == [yY][eE][sS] ]]; }

echo "======================================================"
echo "  Aardvark installer - macOS"
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

_install_python_brew() {
  info "Installing latest stable Python via Homebrew..."
  # Try each version from newest to oldest so we always get the most recent.
  for pyver in "python@3.13" "python@3.12" "python@3.11"; do
    local minor="${pyver#python@}"
    info "  Trying Homebrew $pyver ..."
    brew install "$pyver" 2>/dev/null || brew upgrade "$pyver" 2>/dev/null || true
    local prefix
    prefix="$(brew --prefix "$pyver" 2>/dev/null)"
    local bp="${prefix}/bin/python${minor}"
    if _verify_python "$bp"; then PYTHON_BIN="$bp"; info "Python $minor installed via Homebrew."; return 0; fi
    # Also check unversioned name in case Homebrew added it to PATH
    if _verify_python "python${minor}"; then PYTHON_BIN="python${minor}"; info "Python $minor found in PATH."; return 0; fi
  done
  if _verify_python python3; then PYTHON_BIN="python3"; info "Python found as python3."; return 0; fi
  return 1
}

_install_homebrew() {
  info "Installing Homebrew (you may be prompted for your password)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add Homebrew to PATH for this session (Apple Silicon / Intel)
  if [[ -f /opt/homebrew/bin/brew ]]; then eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -f /usr/local/bin/brew ]];   then eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew >/dev/null 2>&1
}

_ensure_python() {
  if _verify_python "$PYTHON_BIN"; then
    info "Python $("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') found."
    return 0
  fi
  for p in python3.13 python3.12 python3.11 python3 python; do
    if _verify_python "$p"; then PYTHON_BIN="$p"; info "Python found as $p."; return 0; fi
  done

  warn "Python 3.11+ not found."
  if ! ask "Attempt to install the latest stable Python 3.x automatically?"; then
    error "Python 3.11+ is required.  Install it then re-run this installer."
    error "  Homebrew: brew install python@3.13  (or python@3.12 / python@3.11)"
    error "  Official: https://www.python.org/downloads/macos/"
    exit 1
  fi

  # Try Homebrew first
  if command -v brew >/dev/null 2>&1; then
    if _install_python_brew; then return 0; fi
  else
    warn "Homebrew is not installed."
    if ask "Install Homebrew first (needed to auto-install Python)?"; then
      if _install_homebrew; then
        info "Homebrew installed."
        if _install_python_brew; then return 0; fi
        warn "Python install via Homebrew failed."
      else
        warn "Homebrew installation failed."
      fi
    fi
  fi

  # Offer python.org as fallback
  warn "Automatic installation did not succeed."
  echo
  echo "  Please install Python 3.11+ manually:"
  echo "    Option 1 - Homebrew:  brew install python@3.13  (or @3.12 / @3.11)"
  echo "    Option 2 - Official:  https://www.python.org/downloads/macos/"
  echo "      Step 1: Download the macOS .pkg installer from that page"
  echo "      Step 2: Double-click the .pkg file"
  echo "      Step 3: Click Continue, then Agree, then Install"
  echo "      Step 4: Enter your macOS password if prompted"
  echo "      Step 5: Click Close when done"
  echo
  if ask "Open https://www.python.org/downloads/macos/ in your browser now?"; then
    open "https://www.python.org/downloads/macos/"
  fi
  echo
  read -rp "  Press Enter after Python is installed: "
  for p in python3.13 python3.12 python3.11 python3 python; do
    if _verify_python "$p"; then PYTHON_BIN="$p"; info "Python found as $p."; return 0; fi
  done
  error "Python 3.11+ still not found.  Cannot continue."
  exit 1
}

_ensure_rsync() {
  if command -v rsync >/dev/null 2>&1; then info "rsync found."; return 0; fi
  warn "rsync not found (Xcode Command Line Tools are likely missing)."
  if ask "Install Xcode Command Line Tools (includes rsync, git, make)?"; then
    xcode-select --install 2>/dev/null || true
    echo "  A dialog box will appear asking to install the Command Line Tools."
    echo "  Click 'Install', then 'Agree'.  This download takes a few minutes."
    read -rp "  Press Enter once the installation is complete: "
    if command -v rsync >/dev/null 2>&1; then info "rsync found."; return 0; fi
    # Homebrew fallback
    if command -v brew >/dev/null 2>&1; then
      brew install rsync 2>/dev/null && command -v rsync >/dev/null 2>&1 \
        && info "rsync installed via Homebrew." && return 0
    fi
    error "rsync still not found.  To install manually, run:  xcode-select --install"
    exit 1
  else
    error "rsync is required.  Run:  xcode-select --install"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
info "Checking prerequisites..."
_ensure_python
_ensure_rsync


# ---------------------------------------------------------------------------
# Stop existing service before file operations (idempotent)
# ---------------------------------------------------------------------------
launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true
# Wait for relay.py to fully exit (up to 15 s)
_dl=$(( SECONDS + 15 ))
while [[ $SECONDS -lt $_dl ]]; do
  pgrep -f "app/relay.py" > /dev/null 2>&1 || break
  sleep 2
done
pkill -f "$TARGET_DIR/app/relay.py" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Copy application files
# ---------------------------------------------------------------------------
info "Copying application to: $TARGET_DIR"
mkdir -p "$TARGET_DIR"
mkdir -p "$(dirname "$PLIST_FILE")"
rsync -a --delete \
  --exclude '.venv' --exclude 'config.toml' --exclude 'logs/' \
  --exclude 'deltachat_accounts/' --exclude '*.db' \
  --exclude '*.session' --exclude 'relay_state.json' --exclude 'invite_links.txt' \
  "$ROOT_DIR/" "$TARGET_DIR/"

mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Virtual environment and dependencies
# ---------------------------------------------------------------------------
info "Creating Python virtual environment..."
"$PYTHON_BIN" -m venv "$TARGET_DIR/.venv"

ARCH="$(uname -m)"
PLATFORM_WHEELS="$TARGET_DIR/vendor/wheels/macos-$([ "$ARCH" = arm64 ] && echo arm64 || echo x86_64)"
info "Installing dependencies (arch: $ARCH)..."
if ! "$TARGET_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check --no-index --only-binary :all: \
  --find-links "$TARGET_DIR/vendor/wheels/common" \
  --find-links "$PLATFORM_WHEELS" \
  -r "$TARGET_DIR/requirements.txt"; then
  warn "Offline install incomplete.  Falling back to online install for missing packages..."
  if ! "$TARGET_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check \
    --find-links "$TARGET_DIR/vendor/wheels/common" \
    --find-links "$PLATFORM_WHEELS" \
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
# Use arrays so paths with spaces (e.g. "Application Support") are handled correctly.
VALIDATOR=("$TARGET_DIR/.venv/bin/python" "$TARGET_DIR/tools/validate_config.py")
WIZARD=("$TARGET_DIR/.venv/bin/python" "$TARGET_DIR/tools/config_wizard.py" "--output" "$TARGET_DIR/config.toml" "--install-dir" "$TARGET_DIR")

if ! "${VALIDATOR[@]}" "$TARGET_DIR/config.toml" --require-complete >/dev/null 2>&1; then
  echo; warn "Configuration is missing or incomplete.  Starting setup wizard..."; echo
  "${WIZARD[@]}"
fi
if ! "${VALIDATOR[@]}" "$TARGET_DIR/config.toml" --require-complete; then
  error "Configuration is still incomplete.  Re-run the installer to try again."; exit 1
fi
chmod 600 "$TARGET_DIR/config.toml"

# ---------------------------------------------------------------------------
# Firewall (informational)
# ---------------------------------------------------------------------------
FW_STATE=$(defaults read /Library/Preferences/com.apple.alf globalstate 2>/dev/null || echo "-1")
if [[ "$FW_STATE" -ge 1 ]]; then
  info "macOS Application Firewall is active.  Aardvark uses only outbound connections."
  info "No inbound rules are needed.  If macOS asks about Python, click Allow."
fi

# ---------------------------------------------------------------------------
# launchd service
# ---------------------------------------------------------------------------
info "Installing launchd service..."
launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true

cat > "$PLIST_FILE" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$TARGET_DIR/.venv/bin/python</string>
    <string>$TARGET_DIR/app/relay.py</string>
    <string>--config</string>  <string>$TARGET_DIR/config.toml</string>
    <string>--log-level</string> <string>INFO</string>
    <string>--log-file</string>  <string>$LOG_DIR/relay.log</string>
  </array>
  <key>WorkingDirectory</key>  <string>$TARGET_DIR</string>
  <key>KeepAlive</key>         <true/>
  <key>RunAtLoad</key>         <true/>
  <key>StandardOutPath</key>   <string>$LOG_DIR/launchd.stdout.log</string>
  <key>StandardErrorPath</key> <string>$LOG_DIR/launchd.stderr.log</string>
</dict>
</plist>
PLIST_EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST_FILE"
# Stop it immediately so the service does not race with --login.
# _post_install_check will start it after authentication.
launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_FILE"


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
  "$TARGET_DIR/.venv/bin/python" \
    "$TARGET_DIR/app/relay.py" \
    --login --config "$TARGET_DIR/config.toml"
  local _rc=$?; cd "$_prev"; return $_rc
}

_post_install_check() {
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
      # Stop any running instance to avoid session-file conflict during interactive login
      launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true
      sleep 2
      if _run_login; then
        info "Authentication successful. Starting service..."
        launchctl bootstrap "gui/$(id -u)" "$PLIST_FILE"
        launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
        sleep 3
      else
        warn "Authentication was not completed."
        echo "  Run it manually when ready, then start the service:"
        echo "    $TARGET_DIR/.venv/bin/python $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
        echo "  Then: launchctl bootstrap \"gui/$(id -u)\" $PLIST_FILE && launchctl kickstart -k \"gui/$(id -u)/$LABEL\""
        return
      fi
    else
      echo
      echo "  Run authentication manually before the service can work:"
      echo "    $TARGET_DIR/.venv/bin/python \\"
      echo "      $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
      echo "  Then restart the service."
      return
    fi
  fi

  # Session exists - start service now
  info "Telegram session found.  Starting service..."
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true

  info "Waiting for service connections (up to 45 s)..."
  local _w=0
  while [[ $_w -lt 45 ]]; do
    sleep 5; _w=$((_w + 5))
    [[ -f "$LOG_DIR/relay.log" ]] && log=$(tail -300 "$LOG_DIR/relay.log" 2>/dev/null || true)
    echo "$log" | grep -q "Listening for new messages\|Telegram client connected\|Relay service is running" && break
  done

  local log=""
  [[ -f "$LOG_DIR/relay.log" ]] && log=$(tail -300 "$LOG_DIR/relay.log" 2>/dev/null || true)

  echo
  echo "======================================================"
  echo "  Connection status"
  echo "======================================================"

  if echo "$log" | grep -q "Listening for new messages"; then
    info "Telegram :  CONNECTED"
  elif echo "$log" | grep -q "NOT reachable.*proxy\|proxy.*NOT reachable"; then
    warn "Telegram :  PROXY UNREACHABLE"
    echo "  Check [proxy] host/port/secret in config.toml."
  elif echo "$log" | grep -q "Server closed the connection\|ERROR.*Telegram"; then
    warn "Telegram :  CONNECTION FAILED"
    echo "  Check proxy settings and network."
    echo "  Logs: tail -f \"$LOG_DIR/relay.log\""
  else
    warn "Telegram :  UNKNOWN  (still starting - check logs in a moment)"
  fi

  if echo "$log" | grep -q "DC forwarding disabled\|delta_chat.enabled = false"; then
    info "Delta Chat:  DISABLED (email-only mode)"
  elif echo "$log" | grep -q "DC chat ready\|Delta Chat I/O started"; then
    info "Delta Chat:  CONNECTED"
  elif echo "$log" | grep -q "Failed to start Delta Chat\|IMAP failed\|JsonRpcError"; then
    warn "Delta Chat:  CONNECTION FAILED"
    echo "  Check [delta_chat] credentials in config.toml."
    echo "  If behind a proxy, add [dc_proxy] with type=socks5."
  else
    warn "Delta Chat:  UNKNOWN  (still starting or check logs)"
  fi

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
        launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true
        sleep 4
        rm -f "$TARGET_DIR"/*.session
        info "Old session removed."
        if _run_login; then
          info "Authentication successful. Restarting service..."
          launchctl bootstrap "gui/$(id -u)" "$PLIST_FILE" && launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
          sleep 5
        else
          warn "Re-authentication not completed."
          echo "  Run manually, then restart the service:"
          echo "    $TARGET_DIR/.venv/bin/python $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
        fi
      else
        echo "  To fix manually:"
        echo "    1. Stop the service"
        echo "    2. rm \"$TARGET_DIR\"/*.session"
        echo "    3. $TARGET_DIR/.venv/bin/python $TARGET_DIR/app/relay.py --login --config $TARGET_DIR/config.toml"
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
  echo "  Logs: tail -f \"$LOG_DIR/relay.log\""
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
echo "    Status : launchctl print gui/$(id -u)/$LABEL"
echo "    Stop   : launchctl bootout  gui/$(id -u) $PLIST_FILE"
echo "    Start  : launchctl bootstrap gui/$(id -u) $PLIST_FILE"
echo "    Logs   : tail -f \"$LOG_DIR/relay.log\""
echo
echo "  AUTOSTART: starts at user LOGIN, not at system boot."
echo "    The service is a user-level LaunchAgent - it starts when you log in"
echo "    to your macOS account, but not before (not at the login screen)."
echo "    For 24/7 operation without manual login, enable automatic login:"
echo "    System Settings → Users & Groups → Automatic Login"
echo
echo "  Config hot-reload: edit config.toml while running;"
echo "    channel and burst changes apply automatically in ~30 s."
echo
echo "  FIRST-TIME TELEGRAM LOGIN (required before the service can run):"
echo "    $TARGET_DIR/.venv/bin/python \\"
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
