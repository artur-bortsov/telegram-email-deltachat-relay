"""
Telegram → Delta Chat relay service – main entry point.

Usage
-----
    python relay.py [--config CONFIG] [--log-level LEVEL] [--list-channels]

Options
-------
    --config FILE         TOML configuration file (default: config.toml)
    --log-level LEVEL     DEBUG | INFO | WARNING | ERROR (default: INFO)
    --list-channels       Connect to Telegram, print accessible channels,
                          then exit (useful for finding channel @usernames)

The service:
  1. Connects to Telegram with your user credentials (Telethon/MTProto).
  2. Relays the last N (or today's) messages from each watched channel.
  3. Creates a Delta Chat group chat named after each Telegram channel
     and writes the join (invite) links to ``invite_links.txt``.
  4. Forwards every new Telegram channel message to the matching DC group
     (and optionally to a plain e-mail address).
  5. Combines message floods (bursts) into a single relay message.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import traceback
from pathlib import Path
from typing import Optional, Set

__version__ = "1.3.0"   # SemVer: MAJOR.MINOR.PATCH

from relay.admin_notifier import AdminNotifier
from relay.burst_limiter import BurstLimiter
from relay.channel_mapper import ChannelMapper
from relay.config import Config, load_config
from relay.deltachat_client import DeltaChatClient
from relay.email_relay import EmailRelay
from relay.state_tracker import StateTracker
from relay.telegram_client import TelegramMonitor, TelegramSessionInvalidError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Telegram → Delta Chat relay service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--config",
        default="config.toml",
        metavar="FILE",
        help="TOML configuration file (default: config.toml)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="Logging verbosity: DEBUG, INFO, WARNING, ERROR (default: INFO)",
    )
    p.add_argument(
        "--list-channels",
        action="store_true",
        help="List all Telegram channels the account can access, then exit",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a background daemon (Unix/macOS only)",
    )
    p.add_argument(
        "--stop",
        action="store_true",
        help="Stop the running daemon and exit",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Show whether the daemon is running and exit",
    )
    p.add_argument(
        "--login",
        action="store_true",
        help=(
            "Perform interactive Telegram authentication, then exit.  "
            "Run this once before starting the service so the session "
            "file is created.  You will be prompted for the login code "
            "Telegram sends via app, SMS, call, or another available "
            "delivery method, and your Cloud Password if "
            "two-step verification (2FA) is enabled."
        ),
    )
    p.add_argument(
        "--test-admin-notification",
        action="store_true",
        help="Send a test email to admin_notifications.administrator_emails, then exit",
    )
    p.add_argument(
        "--log-file",
        default="logs/relay.log",
        metavar="FILE",
        help="Log file path (default: logs/relay.log).  The directory is created if needed.",
    )
    p.add_argument(
        "--pid-file",
        default="relay.pid",
        metavar="FILE",
        help="PID file path (default: relay.pid)",
    )
    return p.parse_args()


def _setup_logging(
    level: str,
    log_file: Optional[str] = None,
    console: bool = True,
) -> None:
    """
    Configure the root logger.

    *console* controls whether a StreamHandler to stdout is added.  In daemon
    mode this should be False: _daemonize() already redirects stdout to the
    log file, so adding a StreamHandler would duplicate every log line.
    When *log_file* is given a RotatingFileHandler is always attached
    (10 MB per file, 10 files kept = 100 MB max).  The log directory is
    created automatically.
    """
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))

    # Console handler (skipped in daemon mode to avoid double-writing)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # Rotating file handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=10,              # keep up to 10 rotated files
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# Daemon management (Unix/macOS)
# ---------------------------------------------------------------------------

def _daemonize(log_file: str, pid_file: str) -> None:
    """
    Detach from the controlling terminal using the POSIX double-fork technique.

    After this call returns (in the grandchild), stdin is /dev/null and
    stdout/stderr both point to *log_file* (append mode).  The grandchild
    writes its PID to *pid_file*.
    """
    if sys.platform == "win32":
        sys.exit("--daemon is not supported on Windows. Run with 'start /B' or a service manager.")

    sys.stdout.flush()
    sys.stderr.flush()

    # --- First fork ---
    pid = os.fork()
    if pid > 0:
        # Original process: report and exit
        print(f"Relay service starting in background.")
        print(f"  Log : {Path(log_file).resolve()}")
        print(f"  PID : {Path(pid_file).resolve()}  (written after start)")
        print(f"  Stop: python relay.py --stop")
        sys.exit(0)

    os.setsid()   # New session – detach from terminal

    # --- Second fork (prevent daemon from ever re-acquiring a terminal) ---
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect stdin → /dev/null; stdout+stderr → log file
    log_path = Path(log_file).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(os.devnull, "r") as dev_null:
        os.dup2(dev_null.fileno(), sys.stdin.fileno())
    log_fd = open(str(log_path), "a", buffering=1)   # line-buffered
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    log_fd.close()

    # Write PID file
    Path(pid_file).write_text(str(os.getpid()))


def _stop_daemon(pid_file: str) -> None:
    """Send SIGTERM to the running daemon."""
    pid_path = Path(pid_file)
    if not pid_path.exists():
        print("No PID file found – relay service does not appear to be running.")
        return
    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to PID {pid}. The service will stop shortly.")
        pid_path.unlink(missing_ok=True)
    except ProcessLookupError:
        print(f"PID {pid} not found – service was not running. Removing stale PID file.")
        pid_path.unlink(missing_ok=True)


def _status_daemon(pid_file: str) -> None:
    """Print the running status of the daemon."""
    pid_path = Path(pid_file)
    if not pid_path.exists():
        print("Relay service: NOT running (no PID file).")
        return
    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, 0)   # signal 0 = existence check only
        print(f"Relay service: RUNNING (PID {pid})")
    except ProcessLookupError:
        print(f"Relay service: NOT running (stale PID file for PID {pid}).")
        pid_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# List-channels helper
# ---------------------------------------------------------------------------

async def _list_channels(config: Config) -> None:
    """Connect to Telegram and print all accessible channels."""
    from telethon import TelegramClient
    from telethon.tl.types import Channel

    client = TelegramClient(
        config.telegram.session_name,
        config.telegram.api_id,
        config.telegram.api_hash,
    )
    await client.start(phone=config.telegram.phone)
    print("\nAccessible Telegram channels / groups:")
    print("-" * 60)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Channel):
            username = f"@{entity.username}" if entity.username else "(no username)"
            print(f"  id={entity.id:<12} {username:<30} {entity.title}")
    await client.disconnect()
    print("-" * 60)
    print("Add channel usernames or IDs to [channels] watch in config.toml\n")


# ---------------------------------------------------------------------------
# Main relay coroutine
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Relay state – mutable wrapper so that nested closures see config updates
# ---------------------------------------------------------------------------

class _RelayState:
    """Holds mutable references shared across all relay closures."""
    __slots__ = ("config",)

    def __init__(self, config: Config) -> None:
        self.config = config


# ---------------------------------------------------------------------------
# Main relay coroutine
# ---------------------------------------------------------------------------

_FAREWELL = (
    "[Relay stopped]\n"
    "This channel mirror has been removed from the relay configuration. "
    "No further messages will be forwarded here."
)

# ---------------------------------------------------------------------------
# First-time Telegram login
# ---------------------------------------------------------------------------
def _telegram_code_type_description(code_type: object | None) -> str:
    """Return a user-facing description for Telegram's code delivery type."""
    type_name = type(code_type).__name__ if code_type is not None else "unknown"
    descriptions = {
        "SentCodeTypeApp": (
            "Telegram app message "
            "(check the official Telegram chat in an already logged-in app)"
        ),
        "SentCodeTypeSms": "SMS",
        "SentCodeTypeCall": "phone call",
        "SentCodeTypeFlashCall": "flash call",
        "SentCodeTypeMissedCall": "missed call",
        "SentCodeTypeEmailCode": "email",
        "SentCodeTypeSetUpEmailRequired": "email setup required",
        "SentCodeTypeFragmentSms": "Fragment SMS",
        "SentCodeTypeFirebaseSms": "SMS via Firebase/Android",
        "SentCodeTypeSmsWord": "SMS containing a word",
        "SentCodeTypeSmsPhrase": "SMS containing a phrase",
        "CodeTypeSms": "SMS",
        "CodeTypeCall": "phone call",
        "CodeTypeFlashCall": "flash call",
        "CodeTypeMissedCall": "missed call",
        "CodeTypeFragmentSms": "Fragment SMS",
    }
    description = descriptions.get(type_name, type_name)

    details = []
    for attr in ("length", "email_pattern", "prefix", "beginning", "pattern", "url"):
        value = getattr(code_type, attr, None)
        if value:
            details.append(f"{attr.replace('_', ' ')}: {value}")
    if details:
        description = f"{description} ({', '.join(details)})"

    return description


def _print_telegram_code_delivery(sent_code: object) -> None:
    """Print the delivery method Telegram reported for the requested code."""
    delivery_type = getattr(sent_code, "type", None)
    next_type = getattr(sent_code, "next_type", None)
    timeout = getattr(sent_code, "timeout", None)

    print()
    print(f"  Code delivery : {_telegram_code_type_description(delivery_type)}")
    if next_type is not None and timeout is not None:
        print(
            "  Next option   : "
            f"{_telegram_code_type_description(next_type)} in {timeout} seconds"
        )
    elif next_type is not None:
        print(f"  Next option   : {_telegram_code_type_description(next_type)}")
    elif timeout is not None:
        print(f"  Retry timeout : {timeout} seconds")

    print()
    print("  Enter only the Telegram login code when it arrives.")
    print("  Do not enter invite links, recovery codes, or other tokens.")
    print()

def _admin_install_dir(config_path: str) -> Path:
    """Return the install directory implied by the config path."""
    return Path(config_path).expanduser().resolve().parent


def _admin_service_commands(config_path: str, session_name: str) -> str:
    """Return platform-specific commands for administrator emails."""
    install_dir = _admin_install_dir(config_path)
    session_file = Path(session_name)
    if session_file.suffix != ".session":
        session_file = Path(f"{session_file}.session")

    if sys.platform == "darwin":
        plist = "$HOME/Library/LaunchAgents/com.aardvark.relay.plist"
        return (
            "macOS commands:\n"
            f"  1. cd \"{install_dir}\"\n"
            f"  2. launchctl bootout gui/$(id -u) \"{plist}\"\n"
            f"  3. mv \"{session_file}\" \"{session_file}.invalid.$(date +%Y%m%d-%H%M%S)\"\n"
            "  4. .venv/bin/python app/relay.py --login --config config.toml\n"
            f"  5. launchctl bootstrap gui/$(id -u) \"{plist}\"\n"
            "  6. launchctl print gui/$(id -u)/com.aardvark.relay\n"
            "  7. tail -f logs/relay.log"
        )
    if sys.platform.startswith("linux"):
        return (
            "Linux commands:\n"
            "  1. sudo systemctl stop aardvark-relay\n"
            f"  2. cd \"{install_dir}\"\n"
            f"  3. sudo -u aardvark mv \"{session_file}\" \"{session_file}.invalid.$(date +%Y%m%d-%H%M%S)\"\n"
            "  4. sudo -u aardvark .venv/bin/python app/relay.py --login --config config.toml\n"
            "  5. sudo systemctl start aardvark-relay\n"
            "  6. sudo systemctl status aardvark-relay\n"
            "  7. journalctl -u aardvark-relay -f"
        )
    if sys.platform == "win32":
        return (
            "Windows commands (run in an Administrator terminal):\n"
            r"  1. sc stop AardvarkRelay" "\n"
            rf"  2. cd /d \"{install_dir}\"" "\n"
            rf"  3. ren \"{session_file}\" \"{session_file}.invalid.%DATE:/=-%-%TIME::=-%\"" "\n"
            r"  4. .venv\Scripts\python app\relay.py --login --config config.toml" "\n"
            r"  5. sc start AardvarkRelay" "\n"
            r"  6. sc query AardvarkRelay" "\n"
            r"  7. powershell -command Get-Content logs\relay.log -Wait -Tail 50"
        )
    return (
        "Manual commands:\n"
        f"  1. cd \"{install_dir}\"\n"
        "  2. Stop the running service/process.\n"
        f"  3. Move/delete \"{session_file}\" if Telegram re-authentication is required.\n"
        "  4. .venv/bin/python app/relay.py --login --config config.toml\n"
        "  5. Start the service/process again.\n"
        "  6. Check logs/relay.log."
    )


def _telegram_code_retry_note() -> str:
    """Return the delayed retry guidance for Telegram code delivery."""
    return (
        "If the Telegram login code does not arrive within about one minute:\n"
        "  - Press Ctrl+C to cancel the login prompt.\n"
        "  - Do not keep requesting codes repeatedly.\n"
        "  - Try the same login command again about 3 hours later.\n"
        "  - This is Telegram code-delivery throttling/selection, not a relay issue.\n"
        "  - When retrying, check the official Telegram service chat in an already\n"
        "    logged-in app as well as SMS/call/email if Telegram offers them."
    )


def _admin_issue_body(
    title: str,
    summary: str,
    config_path: str,
    *,
    actions: list[str],
    details: str = "",
    include_reauth: bool = False,
    session_name: str = "aardvark",
) -> str:
    """Build a consistent administrator notification body."""
    commands = _admin_service_commands(config_path, session_name)
    body = [
        title,
        "",
        "Summary:",
        summary,
        "",
        "Recommended actions:",
    ]
    body.extend(f"  {i}. {action}" for i, action in enumerate(actions, start=1))
    if include_reauth:
        body.extend(["", _telegram_code_retry_note()])
    body.extend(
        [
            "",
            "Useful commands:",
            commands,
            "",
            "If this is not one of the known issues, collect diagnostics before changing config:",
            "  - Check the service status.",
            "  - Review the latest relay log entries.",
            "  - Verify network/proxy reachability.",
            "  - Verify SMTP/IMAP credentials if Delta Chat or email delivery is affected.",
        ]
    )
    if details:
        body.extend(["", "Technical details:", details])
    return "\n".join(body)


def _admin_telegram_reauth_body(
    exc: TelegramSessionInvalidError,
    config_path: str,
    session_name: str,
) -> str:
    """Build the Telegram re-authentication alert body."""
    commands = _admin_service_commands(config_path, session_name)
    return (
        "Aardvark cannot relay Telegram messages because the saved Telegram "
        "session is not usable.\n\n"
        f"Error:\n{exc}\n\n"
        "This often happens when the same Telethon .session file is reused from "
        "another machine, from a different IP route/VPN/provider, or by two "
        "processes at the same time. Telegram may invalidate the authorization "
        "key. The relay cannot repair this automatically; an administrator must "
        "create a new session with an interactive login.\n\n"
        "Recovery steps:\n"
        f"{commands}\n\n"
        f"{_telegram_code_retry_note()}"
    )


async def _send_admin_notification_test(config: Config, config_path: str) -> bool:
    """Send a test administrator notification and print the result."""
    notifier = AdminNotifier(
        config.admin_notifications,
        config.email_relay,
        proxy_cfg=config.dc_proxy,
    )
    body = _admin_issue_body(
        "Aardvark administrator notification test",
        "This is a test message. No service action is required.",
        config_path,
        actions=[
            "Confirm this email arrived at the configured administrator mailbox.",
            "If it did not arrive, check [admin_notifications] recipients and [email_relay] SMTP settings.",
        ],
    )
    sent = await notifier.send_test(body)
    if sent:
        print("Admin notification test sent.")
    else:
        print(
            "Admin notification test was not sent. Check admin_notifications and email_relay settings.",
            file=sys.stderr,
        )
    return sent


async def _do_login(config: Config) -> None:
    """
    Interactive Telegram authentication for first-time setup.

    Creates a TelegramClient using the same credentials and proxy as the
    relay service, completes the login-code + optional 2FA flow, and saves the
    session file.  After this exits the service can start unattended.
    """
    from telethon import TelegramClient
    from telethon import errors

    # Build proxy the same way the relay does
    tg_proxy: dict = {}
    cfg = config.proxy
    if cfg.enabled and cfg.host:
        if cfg.type == "mtproto":
            try:
                from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
                tg_proxy = {
                    "connection": ConnectionTcpMTProxyRandomizedIntermediate,
                    "proxy": (cfg.host, cfg.port, cfg.password),
                }
            except ImportError:
                logger.warning("MTProto connection class not found; connecting without proxy.")
        else:
            try:
                import socks as _socks
                proxy_type = _socks.SOCKS5 if cfg.type == "socks5" else _socks.HTTP
                tg_proxy = {"proxy": (
                    proxy_type, cfg.host, cfg.port,
                    cfg.rdns, cfg.username or None, cfg.password or None,
                )}
            except ImportError:
                logger.warning("PySocks not installed; connecting without proxy.")

    print()
    print("  Telegram first-time login")
    print("  =========================")
    print(f"  Phone    : {config.telegram.phone}")
    if cfg.enabled and cfg.host:
        print(f"  Proxy    : {cfg.type.upper()} {cfg.host}:{cfg.port}")
    print()
    print("  Telegram will send a login code through the delivery method it chooses.")
    print("  This is often an in-app message from the official Telegram chat,")
    print("  not necessarily SMS. The chosen method will be printed below.")
    print("  If no code arrives within about one minute, press Ctrl+C and")
    print("  try again about 3 hours later. This is not a relay issue.")
    print("  If two-step verification (2FA) is enabled you will also be")
    print("  prompted for your Cloud Password.")
    print()

    client = TelegramClient(
        config.telegram.session_name,
        config.telegram.api_id,
        config.telegram.api_hash,
        **tg_proxy,
    )
    try:
        await client.connect()
        me = await client.get_me()
        if me is None:
            sent_code = await client.send_code_request(config.telegram.phone)
            _print_telegram_code_delivery(sent_code)

            attempts = 0
            two_step_detected = False
            while me is None and attempts < 3 and not two_step_detected:
                code = input("  Telegram login code: ").strip()
                attempts += 1
                try:
                    me = await client.sign_in(config.telegram.phone, code=code)
                except errors.SessionPasswordNeededError:
                    two_step_detected = True
                except (
                    errors.PhoneCodeEmptyError,
                    errors.PhoneCodeExpiredError,
                    errors.PhoneCodeHashEmptyError,
                    errors.PhoneCodeInvalidError,
                ) as exc:
                    print(f"  Invalid or expired code ({type(exc).__name__}). Please try again.")

            if two_step_detected:
                attempts = 0
                while me is None and attempts < 3:
                    password = getpass.getpass("  Telegram Cloud Password (2FA): ")
                    attempts += 1
                    try:
                        me = await client.sign_in(password=password)
                    except errors.PasswordHashInvalidError:
                        print("  Invalid Cloud Password. Please try again.")

            if me is None:
                raise RuntimeError("Telegram login failed after 3 attempts.")

        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        un   = f"@{me.username}" if me.username else "(no username)"
        print()
        print(f"  Authenticated as: {name} {un}")
        print(f"  Session saved  : {config.telegram.session_name}.session")
        print()
        print("  You can now start the Aardvark service.")
    finally:
        await client.disconnect()


async def _run_relay(initial_config: Config, config_path: str) -> None:
    """Set up all subsystems and run the relay until interrupted."""

    state = _RelayState(initial_config)
    admin_notifier = AdminNotifier(
        state.config.admin_notifications,
        state.config.email_relay,
        proxy_cfg=state.config.dc_proxy,
    )

    # --- Channel mapper (invite links → file) ---
    mapper = ChannelMapper(state.config.relay.invite_links_file)

    # --- Burst limiter ---
    burst: BurstLimiter | None = None
    if state.config.burst.enabled:
        burst = BurstLimiter(
            threshold=state.config.burst.threshold,
            window_seconds=state.config.burst.window_seconds,
            separator=state.config.burst.separator,
        )
        logger.info(
            "Burst limiter enabled: ≥%d messages / %d s will be combined",
            state.config.burst.threshold,
            state.config.burst.window_seconds,
        )

    # --- Delta Chat client (optional) ---
    dc_client: DeltaChatClient | None = None
    if state.config.delta_chat is not None and state.config.delta_chat.enabled:
        try:
            dc_client = DeltaChatClient(
                state.config.delta_chat, proxy_cfg=state.config.dc_proxy
            )
            # Run blocking start() in executor to avoid blocking the loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, dc_client.start)
        except Exception as exc:
            logger.exception(
                "Failed to start Delta Chat client – DC forwarding disabled"
            )
            await admin_notifier.notify(
                "delta-chat-start-failed",
                "Delta Chat forwarding failed",
                _admin_issue_body(
                    "Delta Chat forwarding failed",
                    "Delta Chat did not start, so Telegram messages cannot be forwarded to Delta Chat channels.",
                    config_path,
                    actions=[
                        "Check [delta_chat] addr, mail_pw, mail_server, and send_server in config.toml.",
                        "Verify the provider allows IMAP/SMTP access and that an App Password is used when required.",
                        "If the network requires a proxy for email, configure [dc_proxy] with socks5/http.",
                        "Restart the service after fixing credentials or proxy settings.",
                    ],
                    details="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                    session_name=state.config.telegram.session_name,
                ),
            )
            dc_client = None
    elif state.config.delta_chat is not None and not state.config.delta_chat.enabled:
        logger.info(
            "Delta Chat relay is disabled (delta_chat.enabled = false in config). "
            "Only email relay will be used."
        )
    else:
        logger.warning(
            "Delta Chat credentials not configured – "
            "messages will not be forwarded to Delta Chat."
        )

    # --- Email relay ---
    email_relay = EmailRelay(state.config.email_relay, proxy_cfg=state.config.dc_proxy)
    if state.config.email_relay.enabled:
        logger.info(
            "E-mail relay enabled → %s",
            ", ".join(state.config.email_relay.target_emails),
        )

    # --- Track which DC chats have been set up this session ---
    _setup_done: Set[str] = set()
    # Channels whose invite link was None (DC still syncing) and should be retried.
    _invite_pending: Set[str] = set()

    def _setup_channel_sync(channel_name: str) -> None:
        """Create DC chat + register invite link (runs in thread executor)."""
        if dc_client is None:
            return
        invite_link = dc_client.get_invite_link(channel_name)
        mapper.register(channel_name, invite_link)
        if invite_link:
            logger.info(
                "DC chat ready for '%s' – invite link: %s",
                channel_name, invite_link,
            )
            _invite_pending.discard(channel_name)
        else:
            # get_invite_link() returned None (timeout or error).
            # Mark as pending so the retry loop picks it up once DC is ready.
            _invite_pending.add(channel_name)
            logger.info(
                "DC chat for '%s' created; invite link pending (DC still syncing).",
                channel_name,
            )

    async def _ensure_channel_setup(channel_name: str) -> None:
        """Lazy-create the DC group chat and save the invite link."""
        if channel_name in _setup_done:
            return
        _setup_done.add(channel_name)
        if dc_client is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _setup_channel_sync, channel_name)

    async def _retry_pending_invite_links() -> None:
        """
        Background task: re-try invite-link generation for channels where
        get_invite_link() previously timed out (DC was still syncing its mailbox).
        Runs every 60 s and stops retrying once all links are obtained.
        """
        while True:
            await asyncio.sleep(60)
            if dc_client is None or not _invite_pending:
                continue
            pending = list(_invite_pending)
            logger.info(
                "Retrying invite links for %d channel(s): %s",
                len(pending), pending,
            )
            loop = asyncio.get_event_loop()
            for ch_name in pending:
                await loop.run_in_executor(None, _setup_channel_sync, ch_name)

    # --- Core relay callback (called after burst filtering) ---

    async def _relay_message(
        channel_name: str,
        channel_id: str,
        text: str,
        media_path: Optional[str] = None,
    ) -> None:
        """Forward one message (text and/or media) to all configured targets."""
        # Each message goes to a dedicated channel, so no header prefix needed.

        if dc_client is not None:
            await dc_client.send_message(channel_name, text, media_path)

        if state.config.email_relay.enabled:
            await email_relay.send(
                subject=f"[{channel_name}]",
                body=text,
                media_path=media_path,
            )

        # Delete the temporary media file after all targets have received it
        if media_path:
            try:
                parent = os.path.dirname(media_path)
                if os.path.basename(parent).startswith("tg_relay_"):
                    shutil.rmtree(parent, ignore_errors=True)
                else:
                    os.unlink(media_path)
            except Exception:
                pass

    # --- Message handler (Telegram callback, burst-aware) ---

    async def _on_message(
        channel_name: str,
        channel_id: str,
        text: str,
        media_path: Optional[str] = None,
    ) -> None:
        """
        Invoked by TelegramMonitor for every message to relay.

        Messages with media bypass the burst limiter and are sent immediately.
        Text-only messages go through the burst limiter (if enabled).
        """
        # Belt-and-suspenders: skip entirely if there is nothing to relay.
        # The upstream guards in _dispatch_group and _on_new_message should
        # already prevent this, but an extra check here stops any empty
        # callback from causing a blank DC message or a stray burst entry.
        if not text and not media_path:
            logger.warning(
                "_on_message called with empty text and no media for channel %r "
                "– skipping (this is a bug; please report with log context).",
                channel_name,
            )
            return

        await _ensure_channel_setup(channel_name)

        if media_path:
            # Media messages (or combined album groups) bypass the burst limiter
            # and are relayed immediately.  Album grouping is handled upstream in
            # TelegramMonitor before this callback is invoked.
            await _relay_message(channel_name, channel_id, text, media_path)
        elif burst is not None:
            # Closure captures channel_name for correct DC chat after delay
            async def _burst_flush(ch_id: str, combined: str) -> None:
                await _relay_message(channel_name, ch_id, combined)

            await burst.process(channel_id, text, _burst_flush)
        else:
            await _relay_message(channel_name, channel_id, text)

    # --- Telegram monitor ---

    tracker = StateTracker(state.config.relay.state_file)

    tg = TelegramMonitor(
        tg_cfg=state.config.telegram,
        ch_cfg=state.config.channels,
        relay_cfg=state.config.relay,
        state_tracker=tracker,
        proxy_cfg=state.config.proxy,
    )
    tg.set_message_callback(_on_message)

    async def _park_after_unrecoverable_telegram_error(
        exc: TelegramSessionInvalidError,
    ) -> None:
        """
        Keep the service process alive after an unrecoverable Telegram auth error.

        launchd/systemd would otherwise restart the process repeatedly even
        though a duplicated, revoked, or missing Telethon session cannot be
        fixed automatically.  Parking avoids a noisy crash loop and leaves a
        clear log message with the manual re-authentication steps.
        """
        logger.error("Unrecoverable Telegram session error: %s", exc)
        logger.error(
            "Aardvark is parked and will not relay Telegram messages until "
            "the session is recreated.  After re-authenticating, restart the "
            "service to resume normal operation."
        )
        await admin_notifier.notify(
            "telegram-session-invalid",
            "Telegram re-authentication required",
            _admin_telegram_reauth_body(
                exc,
                config_path,
                state.config.telegram.session_name,
            ),
        )
        while True:
            await asyncio.sleep(3600)

    # Startup timeout: if Telethon cannot connect within this window (e.g. network
    # outage at boot time) the process exits so launchd/systemd can restart it later
    # rather than hanging indefinitely inside client.start().
    _STARTUP_TIMEOUT = 120  # seconds
    try:
        await asyncio.wait_for(tg.start(), timeout=_STARTUP_TIMEOUT)
    except TelegramSessionInvalidError as exc:
        if dc_client is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, dc_client.stop)
        await _park_after_unrecoverable_telegram_error(exc)
        return
    except asyncio.TimeoutError:
        logger.error(
            "Startup: Telegram connection timed out after %ds "
            "(network may be down). Exiting for automatic restart.",
            _STARTUP_TIMEOUT,
        )
        await admin_notifier.notify(
            "telegram-startup-timeout",
            "Telegram startup timed out",
            _admin_issue_body(
                "Telegram startup timed out",
                "The relay could not connect to Telegram during startup, so it cannot relay messages.",
                config_path,
                actions=[
                    "Check whether Telegram is reachable from the host.",
                    "Check [proxy] settings if a proxy or VPN route is required.",
                    "Review the relay log and service status.",
                    "Restart the service after network/proxy recovery if it did not recover automatically.",
                ],
                session_name=state.config.telegram.session_name,
            ),
        )
        sys.exit(1)
    except Exception as exc:
        logger.exception("Startup: Telegram client failed before relay could run")
        await admin_notifier.notify(
            "telegram-startup-failed",
            "Telegram startup failed",
            _admin_issue_body(
                "Telegram startup failed",
                "The relay failed before it could start listening for Telegram messages.",
                config_path,
                actions=[
                    "Check service logs for the exact exception.",
                    "Verify Telegram API credentials, network, and [proxy] settings.",
                    "If the error mentions authorization or session, recreate the Telegram session with --login.",
                    "Restart the service after fixing the issue.",
                ],
                details="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                include_reauth=True,
                session_name=state.config.telegram.session_name,
            ),
        )
        raise

    # History replay runs outside the startup timeout: media downloads through
    # slow proxies can take much longer than a normal connection timeout.
    try:
        await tg.relay_history()
    except TelegramSessionInvalidError as exc:
        await tg.stop()
        if dc_client is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, dc_client.stop)
        await _park_after_unrecoverable_telegram_error(exc)
        return

    # --- Helper: sync Telegram channel photo → DC channel ---

    async def _sync_channel_photo(channel_id: int, channel_name: str) -> None:
        """Download the Telegram profile photo and apply it to the DC channel."""
        if dc_client is None:
            return
        fingerprint = tg.get_channel_photo_fingerprint(channel_id)
        if fingerprint is None:
            return
        if tracker.get_photo_fingerprint(str(channel_id)) == fingerprint:
            logger.info(
                "Telegram profile photo unchanged for %r – skipping sync",
                channel_name,
            )
            return
        photo_path = await tg.get_channel_photo(channel_id)
        if photo_path:
            await dc_client.update_channel_info_async(channel_name, photo_path)
            tracker.update_photo_fingerprint(str(channel_id), fingerprint)
            try:
                parent = os.path.dirname(photo_path)
                if os.path.basename(parent).startswith("tg_relay_photo_"):
                    shutil.rmtree(parent, ignore_errors=True)
                else:
                    os.unlink(photo_path)
            except Exception:
                pass

    # --- Pre-create DC channels; send farewell to stale ones; sync photos ---

    monitored_names = set(tg.get_channel_names().values())

    # On startup: notify DC channels that are no longer in config
    if dc_client is not None:
        loop = asyncio.get_event_loop()
        existing_names = await loop.run_in_executor(None, dc_client.get_all_broadcast_names)
        for stale_name in set(existing_names) - monitored_names:
            logger.info("Sending farewell to stale DC channel: %r", stale_name)
            await dc_client.send_message(stale_name, _FAREWELL)

    for channel_id, channel_name in tg.get_channel_names().items():
        await _ensure_channel_setup(channel_name)
        await _sync_channel_photo(channel_id, channel_name)

    # --- Config hot-reload ---

    async def _apply_config_delta(new_cfg: Config) -> None:
        """Apply differences between the current and new config (channels + burst)."""
        added, removed = await tg.update_channels(new_cfg.channels.watch)

        # Farewell to removed channels
        for _eid, ch_name in removed:
            logger.info("Channel removed from config, sending farewell: %r", ch_name)
            if dc_client is not None:
                await dc_client.send_message(ch_name, _FAREWELL)
            if new_cfg.email_relay.enabled:
                await email_relay.send(
                    subject=f"Relay stopped – {ch_name}", body=_FAREWELL
                )
            _setup_done.discard(ch_name)

        # Setup and photo-sync for newly added channels
        for ch_id, ch_name in added:
            await _ensure_channel_setup(ch_name)
            await _sync_channel_photo(ch_id, ch_name)

        # Live-update burst settings
        if burst is not None:
            burst.threshold     = new_cfg.burst.threshold
            burst.window        = new_cfg.burst.window_seconds
            burst.separator     = new_cfg.burst.separator

        state.config = new_cfg
        logger.info(
            "Config reloaded: +%d channel(s) added, -%d channel(s) removed.",
            len(added), len(removed),
        )

    async def _watch_config() -> None:
        """Poll config.toml every 30 s; reload when its mtime changes."""
        last_mtime = os.path.getmtime(config_path)
        while True:
            await asyncio.sleep(30)
            try:
                mtime = os.path.getmtime(config_path)
                if mtime == last_mtime:
                    continue
                last_mtime = mtime
                logger.info("config.toml changed – reloading …")
                try:
                    new_cfg = load_config(config_path)
                    await _apply_config_delta(new_cfg)
                except TelegramSessionInvalidError:
                    raise
                except Exception:
                    logger.exception(
                        "Config reload failed – keeping current configuration"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in config watcher")

    # --- Graceful shutdown via SIGINT / SIGTERM ---

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info("Received %s – initiating shutdown …", sig.name)
        stop_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal, sig)
    else:
        # Windows: asyncio signal handler is only SIGINT (Ctrl-C)
        signal.signal(signal.SIGINT, lambda s, f: loop.call_soon_threadsafe(stop_event.set))

    # ---------------------------------------------------------------------------
    # Watchdog: comprehensive health checks every 5 minutes
    # ---------------------------------------------------------------------------
    _WATCHDOG_CHECK_INTERVAL = 5 * 60    # check every 5 minutes
    _WATCHDOG_IDLE_THRESHOLD = 6 * 3600  # reconnect after 6 h of channel silence
    _DC_HEALTH_TIMEOUT       = 30        # seconds before DC is considered hung
    # Telegram primary DC – used for network reachability check
    _TG_DC1_HOST = "149.154.167.51"
    _TG_DC1_PORT = 443

    async def _check_network() -> bool:
        """Return True when a TCP connection to Telegram DC1 can be established."""
        _loop = asyncio.get_event_loop()
        return await _loop.run_in_executor(
            None, TelegramMonitor._tcp_reachable,
            _TG_DC1_HOST, _TG_DC1_PORT, 5.0,
        )

    async def _check_dc_health() -> bool:
        """
        Return True when the Delta Chat RPC server responds within the timeout.
        Calls get_all_broadcast_names() which is a lightweight RPC round-trip.
        Returns True when DC is disabled (not a failure).
        """
        if dc_client is None:
            return True
        _loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                _loop.run_in_executor(None, dc_client.get_all_broadcast_names),
                timeout=_DC_HEALTH_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            logger.error(
                "Watchdog: Delta Chat RPC did not respond in %ds – DC is hung.",
                _DC_HEALTH_TIMEOUT,
            )
            return False
        except Exception:
            logger.exception("Watchdog: Delta Chat health check failed")
            return False

    # ---------------------------------------------------------------------------
    # Periodic Delta Chat blob-cache cleanup
    # ---------------------------------------------------------------------------
    # The Delta Chat blob directory (`<account>/dc.db-blobs/`) accumulates every
    # outgoing media attachment.  Since this relay is one-way, the sender does
    # not need to keep them around once SMTP delivery has succeeded.  A
    # background task removes blobs older than `delta_chat.cache_lifetime_hours`
    # to bound disk usage on long-running installations.
    _BLOB_CLEANUP_INTERVAL = 60 * 60   # run hourly

    async def _blob_cache_cleanup() -> None:
        """Run cleanup once at startup and then on a fixed interval."""
        if dc_client is None:
            # Keep this task alive.  The main loop uses FIRST_COMPLETED, so a
            # helper task returning would otherwise stop the entire relay.
            await asyncio.Event().wait()
        # Pull the configured lifetime once; a non-positive value disables
        # the task entirely (cleanup_blob_cache() also no-ops in that case,
        # but skipping the loop avoids needless wake-ups).
        lifetime = getattr(state.config.delta_chat, "cache_lifetime_hours", 0) or 0
        if lifetime <= 0:
            logger.info(
                "DC blob cache cleanup disabled (cache_lifetime_hours=%d).",
                lifetime,
            )
            # Keep this task alive for the same reason as the dc_client=None
            # branch above.
            await asyncio.Event().wait()
        logger.info(
                "DC blob cache cleanup enabled: removing files older than %d h "
                "every %d min.",
                lifetime, _BLOB_CLEANUP_INTERVAL // 60,
        )
        _loop = asyncio.get_event_loop()
        try:
            await _loop.run_in_executor(None, dc_client.cleanup_blob_cache)
        except Exception:
            logger.exception("Initial DC blob cache cleanup failed")
        while True:
            await asyncio.sleep(_BLOB_CLEANUP_INTERVAL)
            try:
                await _loop.run_in_executor(None, dc_client.cleanup_blob_cache)
            except Exception:
                logger.exception("DC blob cache cleanup failed")

    async def _watchdog() -> None:
        """Comprehensive health check: network, Telegram connection, idle staleness, DC."""
        while True:
            await asyncio.sleep(_WATCHDOG_CHECK_INTERVAL)

            # 1. Network reachability – skip all reconnect attempts when offline
            network_ok = await _check_network()
            if not network_ok:
                logger.warning(
                    "Watchdog: Telegram servers unreachable (network down?) "
                    "-- skipping checks until next cycle."
                )
                await admin_notifier.notify(
                    "telegram-network-unreachable",
                    "Telegram network unreachable",
                    _admin_issue_body(
                        "Telegram network unreachable",
                        "The watchdog could not reach Telegram. Relay delivery may be stopped until network access returns.",
                        config_path,
                        actions=[
                            "Check internet connectivity from the service host.",
                            "Check VPN/proxy routing if Telegram requires a proxy on this network.",
                            "Review logs to confirm whether the service recovered on the next watchdog cycle.",
                        ],
                        session_name=state.config.telegram.session_name,
                    ),
                )
                continue

            # 2. Telegram connection liveness – reconnect immediately if disconnected
            if not tg.is_connected():
                logger.warning(
                    "Watchdog: Telegram client is not connected -- reconnecting now."
                )
                try:
                    await tg.reconnect()
                except TelegramSessionInvalidError:
                    raise
                except Exception as exc:
                    logger.exception("Watchdog: Telegram reconnect failed -- will retry")
                    await admin_notifier.notify(
                        "telegram-reconnect-failed",
                        "Telegram reconnect failed",
                        _admin_issue_body(
                            "Telegram reconnect failed",
                            "The watchdog detected a disconnected Telegram client and could not reconnect it.",
                            config_path,
                            actions=[
                                "Check network/proxy reachability.",
                                "Check whether the Telegram session was revoked or invalidated.",
                                "If authorization is suspected, stop the service and run --login to recreate the session.",
                            ],
                            details="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                            include_reauth=True,
                            session_name=state.config.telegram.session_name,
                        ),
                    )
                continue  # re-check DC on the next cycle after reconnect settles

            # 3. Update-state staleness – reconnect after 6 h of silence
            idle = tg.get_min_idle_seconds()
            if idle >= _WATCHDOG_IDLE_THRESHOLD:
                logger.warning(
                    "Watchdog: all watched channels idle for %.1f h "
                    "-- forcing Telegram reconnect to re-sync update state.",
                    idle / 3600,
                )
                try:
                    await tg.reconnect()
                except TelegramSessionInvalidError:
                    raise
                except Exception as exc:
                    logger.exception("Watchdog: Telegram reconnect failed -- will retry")
                    await admin_notifier.notify(
                        "telegram-staleness-reconnect-failed",
                        "Telegram staleness reconnect failed",
                        _admin_issue_body(
                            "Telegram staleness reconnect failed",
                            "The watchdog tried to refresh Telegram update state after extended silence and reconnect failed.",
                            config_path,
                            actions=[
                                "Check whether Telegram is reachable.",
                                "Check [proxy] if the route changed or VPN/provider changed.",
                                "If logs mention authorization/session, recreate the session with --login.",
                            ],
                            details="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                            include_reauth=True,
                            session_name=state.config.telegram.session_name,
                        ),
                    )
                continue

            # 4. Delta Chat health – exit cleanly so launchd/systemd can restart
            dc_ok = await _check_dc_health()
            if not dc_ok:
                logger.error(
                    "Watchdog: Delta Chat is unresponsive. "
                    "Exiting for automatic service restart."
                )
                await admin_notifier.notify(
                    "delta-chat-unresponsive",
                    "Delta Chat is unresponsive",
                    _admin_issue_body(
                        "Delta Chat is unresponsive",
                        "The Delta Chat RPC server stopped responding. The relay is exiting so the service manager can restart it.",
                        config_path,
                        actions=[
                            "Check whether the service restarts and Delta Chat recovers automatically.",
                            "If it repeats, inspect logs and the Delta Chat account directory.",
                            "Verify IMAP/SMTP connectivity and [dc_proxy] settings.",
                        ],
                        session_name=state.config.telegram.session_name,
                    ),
                )
                sys.exit(1)

    logger.info("Relay service is running.  Press Ctrl+C to stop.")

    # Run until Telegram disconnects or a shutdown signal is received
    run_task      = asyncio.ensure_future(tg.run_forever())
    stop_task     = asyncio.ensure_future(stop_event.wait())
    watch_task    = asyncio.ensure_future(_watch_config())
    invite_task   = asyncio.ensure_future(_retry_pending_invite_links())
    watchdog_task = asyncio.ensure_future(_watchdog())
    blob_task     = asyncio.ensure_future(_blob_cache_cleanup())

    _done, _pending = await asyncio.wait(
        [run_task, stop_task, watch_task, invite_task, watchdog_task, blob_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    fatal_telegram_error: TelegramSessionInvalidError | None = None
    unexpected_error: BaseException | None = None
    for task in _done:
        if task.cancelled():
            continue
        exc = task.exception()
        if isinstance(exc, TelegramSessionInvalidError):
            fatal_telegram_error = exc
        elif exc is not None:
            unexpected_error = exc
    for task in _pending:
        task.cancel()

    # --- Teardown ---
    await tg.stop()
    if dc_client is not None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, dc_client.stop)
    logger.info("Relay service stopped.")

    if fatal_telegram_error is not None:
        await _park_after_unrecoverable_telegram_error(fatal_telegram_error)
    if unexpected_error is not None:
        await admin_notifier.notify(
            "relay-unexpected-error",
            "Relay stopped after unexpected error",
            _admin_issue_body(
                "Relay stopped after unexpected error",
                "A relay task ended with an unexpected exception. The service may not relay messages until it is restarted and the cause is fixed.",
                config_path,
                actions=[
                    "Review the traceback below and the full relay log.",
                    "Check service status and whether the service manager restarted the process.",
                    "If the cause is unclear, collect logs before changing configuration.",
                ],
                details="".join(
                    traceback.format_exception(
                        type(unexpected_error),
                        unexpected_error,
                        unexpected_error.__traceback__,
                    )
                ),
                session_name=state.config.telegram.session_name,
            ),
        )
        raise unexpected_error


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # --- Daemon control commands (no config needed) ---
    if args.stop:
        _stop_daemon(args.pid_file)
        return
    if args.status:
        _status_daemon(args.pid_file)
        return

    # --- First-time Telegram login (interactive, then exit) ---
    if args.login:
        _setup_logging(args.log_level, console=True)
        config = load_config(args.config)
        asyncio.run(_do_login(config))
        return

    # --- Daemonize BEFORE any asyncio / logging / network setup ---
    if args.daemon:
        _daemonize(args.log_file, args.pid_file)
        # From here we are in the detached grandchild process.
        # stdout/stderr are redirected to relay.log.

    # In daemon mode stdout is already redirected to the log file by _daemonize(),
    # so we attach only the RotatingFileHandler and skip the console handler.
    _setup_logging(args.log_level, log_file=args.log_file, console=not args.daemon)

    logger.info("Aardvark v%s starting", __version__)
    logger.info("Loading configuration from %s", args.config)
    config = load_config(args.config)

    # Windows: use the Selector event loop (ProactorEventLoop has known issues
    # with some asyncio features used by Telethon)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if args.list_channels:
        asyncio.run(_list_channels(config))
        return
    if args.test_admin_notification:
        ok = asyncio.run(_send_admin_notification_test(config, args.config))
        if not ok:
            sys.exit(1)
        return

    asyncio.run(_run_relay(config, args.config))


if __name__ == "__main__":
    main()
