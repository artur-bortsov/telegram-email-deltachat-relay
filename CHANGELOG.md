# Changelog

All notable changes to Aardvark are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [1.1.0] – 2026-04-12

### Added
- **Staleness watchdog** — automatically reconnects to Telegram when all watched
  channels have been silent for 6 hours, recovering silently from a known
  Telegram update-state drift (pts desync) without a service restart.
  Checks run every 5 minutes and also verify network reachability and
  Delta Chat responsiveness.
- **Startup timeout** — if Telethon cannot connect within 120 seconds at boot
  (e.g. network outage), the process exits cleanly so the service manager can
  restart it, instead of hanging indefinitely inside the connection loop.
- **`_ensure_venv` prerequisite check** (Linux installer) — auto-installs
  `python3.X-venv` via `apt`/`dnf`/`yum` when the `venv` module is missing,
  which is common on fresh Ubuntu installs.
- **`PySocks` bundled** in `vendor/wheels/common/` — SOCKS5/HTTP proxy support
  now works offline without a PyPI fallback.
- **`is_connected()` method** on `TelegramMonitor` — used by the watchdog for
  liveness detection.
- **`relay_history()` public method** on `TelegramMonitor` — separates history
  replay from the connection phase so slow media downloads over proxies no
  longer trigger the startup timeout.
- **Version constant** `__version__ = "1.1.0"` logged at startup for easier
  support and log triage.
- **Setup wizard improvements** — detailed parameter explanations, explicit
  default values ("press Enter to accept"), IMAP/SMTP server examples and
  App Password guidance per provider, pre-proxy connectivity check (direct
  Telegram + email test with re-test loop), post-proxy reachability check with
  option to re-specify.
- **Quick start guides** (EN, RU, ES, DE, ZH) — added IMAP/SMTP server
  hostname table and App Password explanation per provider.
- **README** — "Before you run the installer" preparation checklist; new
  "Running the installer: source folder vs. install directory" section
  explaining the difference and Windows maintenance mode for all platforms.

### Fixed
- **DC `configure()` re-run when `is_configured=False`** — previously a failed
  `configure()` (wrong password, IMAP disabled) stored the address and was
  silently skipped on every restart, leaving the DC account permanently broken.
  Now `configure()` is retried whenever `is_configured=False`, even if the
  address matches.
- **History replay outside startup timeout** — downloading many historical
  media messages through a slow MTProto proxy could exceed the 120-second
  startup deadline. History replay now runs after the timeout window.
- **Bash 3.2 compatibility** in macOS installers — `${var,,}` (lowercase
  substitution) is a bash 4.0+ feature unavailable on macOS's built-in bash.
  Replaced with portable glob patterns `[[ "$v" == [yY] || ... ]]`.
- **`local` outside function** — `local _dl=...` at top-level script scope
  caused bash to silently discard the value, breaking the process-exit wait
  loop. Fixed by removing `local`.
- **Path-with-spaces** in `VALIDATOR` / `WIZARD` shell variables — paths
  containing spaces (e.g. macOS `Application Support`) caused word-splitting
  when the variables were expanded unquoted. Fixed by converting both to
  bash arrays.
- **Session-file race** on first install (macOS) — the service was bootstrapped
  before `--login` ran, causing both processes to compete for the same session
  file. Fixed by adding `bootout + sleep` before the login step.
- **macOS `deltachat_accounts/` deletion** — `rsync --delete` was removing the
  DC accounts directory because it was missing from the exclusion list. Fixed
  by adding `deltachat_accounts/` and `*.db` to the exclude lists in all three
  platform installers.

---

## [1.0.0] – 2026-03-30

Initial public release.

### Features
- Telegram → Delta Chat broadcast channel relay (MTProto via Telethon)
- Telegram → plain email relay (SMTP)
- Dual proxy architecture: `[proxy]` for Telegram (mtproto/socks5/http),
  `[dc_proxy]` for Delta Chat and email (socks5/http)
- Hot-reload of `config.toml` while running (channels and burst settings)
- Burst limiter: combines rapid text floods into a single message
- Media relay with configurable size limit; album grouping with dispatch lock
- `relay_state.json` watermarks prevent duplicate messages after restart
- DC channel profile photo sync from Telegram
- Invite-link retry loop for slow IMAP mailbox sync
- Corrupted/expired session detection with guided re-authentication
- Idempotent installers for Linux (systemd), macOS (launchd), Windows (WinSW)
- Offline-capable via bundled `vendor/wheels/` packages
- Interactive setup wizard (`tools/config_wizard.py`) with validation
- Windows maintenance mode when installer is run from the install directory
