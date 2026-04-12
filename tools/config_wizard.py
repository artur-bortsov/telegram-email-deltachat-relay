#!/usr/bin/env python3
"""
Aardvark configuration wizard.

Guides the user step-by-step through setting up config.toml.
Designed to be beginner-friendly: explains every field, shows default
values (press Enter to accept), validates input, tests connectivity
before and after proxy setup, and at the end shows how to control
the service.
"""

from __future__ import annotations

import argparse
import getpass
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hr(char: str = "-", width: int = 60) -> None:
    print(char * width)


def _section(title: str) -> None:
    print()
    _hr("=")
    print(f"  {title}")
    _hr("=")


def _info(text: str) -> None:
    for line in text.strip().splitlines():
        print(f"  {line}")


def _ok(text: str) -> None:
    print(f"  \u2713 {text}")


def _warn(text: str) -> None:
    print(f"  \u26a0  {text}")


def _load_existing(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _ask(
    prompt: str,
    default: Optional[str] = None,
    validator=None,
    secret: bool = False,
    allow_empty: bool = False,
) -> str:
    """Prompt with optional default and validation; re-prompt on invalid input.

    When *default* is set the prompt shows [default value].
    The user can press Enter to accept it without retyping.
    """
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = (
            getpass.getpass(f"{prompt}{suffix}: ")
            if secret
            else input(f"{prompt}{suffix}: ")
        ).strip()
        if not raw:
            if default is not None:
                raw = default
            elif allow_empty:
                return ""
        if validator is not None:
            err = validator(raw)
            if err is not None:
                print(f"  \u2717 {err}")
                continue
        return raw


def _ask_yn(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question, return bool."""
    while True:
        raw = input(f"{prompt} [{'YES/no' if default else 'yes/NO'}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("  \u2717 Please enter yes or no.")


# ---------------------------------------------------------------------------
# Connectivity checks
# ---------------------------------------------------------------------------

def _tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    """Return True when a TCP connection to host:port succeeds."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False


# Telegram data-centres (try multiple for robustness)
_TG_DCS: List[Tuple[str, int]] = [
    ("149.154.167.51", 443),
    ("149.154.167.91", 443),
    ("91.108.4.167",   443),
]


def _check_telegram_direct() -> bool:
    """Return True when any Telegram DC is reachable without a proxy."""
    for host, port in _TG_DCS:
        if _tcp_check(host, port):
            return True
    return False


def _show_connectivity_results(
    tg_ok: bool,
    imap_host: str = "",
    imap_ok: Optional[bool] = None,
    smtp_host: str = "",
    smtp_ok: Optional[bool] = None,
) -> None:
    print()
    _info("Connectivity test (direct, without proxy):")
    if tg_ok:
        _ok("Telegram servers  \u2192  REACHABLE")
    else:
        _warn("Telegram servers  \u2192  UNREACHABLE (direct access may be blocked)")
    if imap_ok is not None:
        label = f"IMAP  {imap_host}:993"
        if imap_ok:
            _ok(f"{label}  \u2192  REACHABLE")
        else:
            _warn(f"{label}  \u2192  UNREACHABLE")
    if smtp_ok is not None:
        label = f"SMTP  {smtp_host}:465"
        if smtp_ok:
            _ok(f"{label}  \u2192  REACHABLE")
        else:
            _warn(f"{label}  \u2192  UNREACHABLE (try port 587 if 465 is blocked)")
    print()


def _connectivity_check_loop(
    imap_host: str = "",
    smtp_host: str = "",
) -> Tuple[bool, Optional[bool], Optional[bool]]:
    """
    Test direct connectivity and allow the user to retry (e.g. after
    enabling a system VPN or changing the network route).

    Returns (tg_ok, imap_ok, smtp_ok).
    imap_ok / smtp_ok are None when not applicable.
    """
    while True:
        print()
        _info("Testing direct connectivity \u2026 (this may take a few seconds)")
        tg_ok = _check_telegram_direct()
        imap_ok: Optional[bool] = None
        smtp_ok: Optional[bool] = None
        if imap_host:
            imap_ok = _tcp_check(imap_host, 993)
        if smtp_host:
            smtp_ok = _tcp_check(smtp_host, 465)

        _show_connectivity_results(tg_ok, imap_host, imap_ok, smtp_host, smtp_ok)

        if _ask_yn(
            "Test again? (e.g. after connecting a VPN or changing the route)",
            default=False,
        ):
            continue
        return tg_ok, imap_ok, smtp_ok


def _show_proxy_check(host: str, port: int) -> bool:
    """TCP-check the proxy and print the result. Returns True if reachable."""
    print()
    _info(f"Testing proxy {host}:{port} \u2026")
    ok = _tcp_check(host, port)
    if ok:
        _ok(f"Proxy {host}:{port}  \u2192  REACHABLE")
    else:
        _warn(f"Proxy {host}:{port}  \u2192  UNREACHABLE (check the address, port, or proxy status)")
    print()
    return ok


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _v_nonempty(v: str) -> Optional[str]:
    return None if v else "value cannot be empty"


def _v_int(v: str) -> Optional[str]:
    if not v:
        return "value cannot be empty"
    try:
        int(v)
        return None
    except ValueError:
        return "please enter a whole number"


def _v_positive_int(v: str) -> Optional[str]:
    err = _v_int(v)
    if err:
        return err
    return None if int(v) > 0 else "must be greater than zero"


def _v_float_ge0(v: str) -> Optional[str]:
    if not v:
        return "value cannot be empty"
    try:
        f = float(v)
        return None if f >= 0 else "must be 0 or positive"
    except ValueError:
        return "please enter a number"


def _v_phone(v: str) -> Optional[str]:
    if not v:
        return "phone cannot be empty"
    return None if re.fullmatch(r"\+?[0-9]{7,20}", v) else "use international format, e.g. +12025551234"


def _v_api_hash(v: str) -> Optional[str]:
    return None if re.fullmatch(r"[0-9a-fA-F]{32}", v) else "must be a 32-character hex string (only 0-9, a-f)"


def _v_email(v: str) -> Optional[str]:
    return None if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) else "not a valid email address"


def _v_channel(v: str) -> Optional[str]:
    if re.fullmatch(r"@[\w\d_]{3,}", v):
        return None
    if v.startswith("t.me/") and len(v) > 5:
        return None
    if re.fullmatch(r"-?\d+", v):
        return None
    return "use @username, t.me/username, or a numeric channel ID"


def _v_ssl_mode(v: str) -> Optional[str]:
    return None if v in {"ssl", "starttls", "none"} else "enter ssl, starttls, or none"


def _v_proxy_type(v: str) -> Optional[str]:
    return None if v in {"socks5", "http", "mtproto"} else "enter socks5, http, or mtproto"


def _v_port(v: str) -> Optional[str]:
    err = _v_int(v)
    if err:
        return err
    p = int(v)
    return None if 1 <= p <= 65535 else "port must be between 1 and 65535"


# ---------------------------------------------------------------------------
# Multi-entry prompts
# ---------------------------------------------------------------------------

def _ask_channels(existing: List[str]) -> List[str]:
    _info(
        "Enter the Telegram channels you want to mirror.\n"
        "\n"
        "Accepted formats:\n"
        "  @username          public channel or group (most common)\n"
        "  t.me/username      t.me link\n"
        "  1234567890         numeric channel ID\n"
        "\n"
        "How to find channel names:\n"
        "  Open the channel in Telegram and check the link, e.g.\n"
        "  t.me/nexta_live  \u2192  enter @nexta_live\n"
        "\n"
        "Tip: run  python app/relay.py --list-channels  to see all channels\n"
        "your account can access.\n"
        "\n"
        "Enter one channel or multiple separated by commas.\n"
        "Example:  @channel1, @channel2"
    )
    default = ", ".join(existing) if existing else ""
    while True:
        raw = _ask("Channels", default=default or None)
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if not items:
            print("  \u2717 Please enter at least one channel.")
            continue
        errors = [_v_channel(i) for i in items]
        if any(errors):
            for i, err in zip(items, errors):
                if err:
                    print(f"  \u2717 {i!r}: {err}")
            continue
        return items


def _ask_emails(prompt: str, existing: List[str], required: bool = False) -> List[str]:
    default = ", ".join(existing) if existing else ""
    while True:
        raw = _ask(prompt, default=default or None, allow_empty=not required)
        if not raw:
            if required:
                print("  \u2717 Please enter at least one email address.")
                continue
            return []
        items = [x.strip() for x in raw.split(",") if x.strip()]
        errors = [_v_email(t) for t in items]
        if any(errors):
            for t, err in zip(items, errors):
                if err:
                    print(f"  \u2717 {t!r}: {err}")
            continue
        return items


# ---------------------------------------------------------------------------
# TOML writer
# ---------------------------------------------------------------------------

def _toml_arr(values: List[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _write_config(path: Path, d: Dict[str, Any], install_dir: Optional[str] = None) -> None:
    """
    Write config.toml.  When *install_dir* is provided, all file-path
    values that are relative are resolved to absolute paths under that
    directory.
    """
    dc = d["delta_chat"]
    em = d["email_relay"]
    pr = d["proxy"]
    rel = d["relay"]
    burst = d["burst"]

    if install_dir:
        idir = Path(install_dir)
        def _abs(p: str) -> str:
            fp = Path(p)
            return str(idir / fp).replace("\\", "/") if not fp.is_absolute() else str(fp).replace("\\", "/")
        rel = dict(rel)
        rel["invite_links_file"] = _abs(rel["invite_links_file"])
        rel["state_file"]         = _abs(rel["state_file"])
        if dc.get("database_path"):
            dc = dict(dc)
            dc["database_path"] = _abs(dc["database_path"])
        tg = dict(d["telegram"])
        tg["session_name"] = _abs(tg["session_name"])
        d = dict(d)
        d["telegram"] = tg

    dc_enabled_str = "true" if dc["enabled"] else "false"
    em_enabled_str = "true" if em["enabled"] else "false"
    pr_enabled_str = "true" if pr["enabled"] else "false"
    burst_enabled_str = "true" if burst["enabled"] else "false"
    pr_rdns_str = "true" if pr.get("rdns", True) else "false"
    pr_use_dc_str = "true" if pr.get("use_for_dc", True) else "false"
    pr_use_em_str = "true" if pr.get("use_for_email", True) else "false"

    content = f"""\
# Aardvark configuration – generated by config_wizard.py
# The service reloads this file while running (channel/burst changes take
# effect immediately; other changes require a service restart).

[telegram]
api_id       = {d['telegram']['api_id']}
api_hash     = "{d['telegram']['api_hash']}"
phone        = "{d['telegram']['phone']}"
session_name = "{d['telegram']['session_name']}"

[channels]
watch = {_toml_arr(d['channels']['watch'])}

[delta_chat]
enabled       = {dc_enabled_str}
addr          = "{dc['addr']}"
mail_pw       = "{dc['mail_pw']}"
database_path = "{dc['database_path']}"
"""
    if dc.get("mail_server"):
        content += f'mail_server = "{dc["mail_server"]}"\n'
    if dc.get("send_server"):
        content += f'send_server = "{dc["send_server"]}"\n'

    content += f"""
[relay]
history_mode         = "{rel['history_mode']}"
history_last_n       = {rel['history_last_n']}
invite_links_file    = "{rel['invite_links_file']}"
auto_create          = true
max_media_size_mb    = {rel['max_media_size_mb']}
state_file           = "{rel['state_file']}"
album_mode           = "{rel['album_mode']}"
album_window_seconds = {rel['album_window_seconds']}

[burst]
enabled        = {burst_enabled_str}
threshold      = {burst['threshold']}
window_seconds = {burst['window_seconds']}
separator      = "\\n---\\n"

[proxy]
enabled       = {pr_enabled_str}
type          = "{pr['type']}"
host          = "{pr['host']}"
port          = {pr['port']}
username      = "{pr['username']}"
password      = "{pr['password']}"
rdns          = {pr_rdns_str}
use_for_dc    = {pr_use_dc_str}
use_for_email = {pr_use_em_str}
"""
    dcp = d.get("dc_proxy")
    if dcp and dcp.get("enabled"):
        dcp_rdns = "true" if dcp.get("rdns", True) else "false"
        dcp_udc  = "true" if dcp.get("use_for_dc", True) else "false"
        dcp_uem  = "true" if dcp.get("use_for_email", True) else "false"
        content += f"""
[dc_proxy]
enabled       = true
type          = "{dcp['type']}"
host          = "{dcp['host']}"
port          = {dcp['port']}
username      = "{dcp.get('username', '')}"
password      = "{dcp.get('password', '')}"
rdns          = {dcp_rdns}
use_for_dc    = {dcp_udc}
use_for_email = {dcp_uem}
"""
    content += f"""
[email_relay]
enabled       = {em_enabled_str}
smtp_host     = "{em['smtp_host']}"
smtp_port     = {em['smtp_port']}
smtp_user     = "{em['smtp_user']}"
smtp_password = "{em['smtp_password']}"
ssl_mode      = "{em['ssl_mode']}"
target_emails = {_toml_arr(em['target_emails'])}
from_name     = "{em['from_name']}"
"""
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Post-setup instructions
# ---------------------------------------------------------------------------

def _print_post_setup(output: Path, install_dir: Optional[str], relay_mode: str) -> None:
    _section("Setup complete!")
    _info(f"Configuration written to: {output.resolve()}")
    print()

    if relay_mode in {"dc", "both"}:
        _info(
            "Delta Chat invite links\n"
            "----------------------\n"
            "After the service starts, invite links for your Delta Chat channels\n"
            "will be written to:  invite_links.txt\n"
            "(or the path configured in relay.invite_links_file)\n"
            "\n"
            "IMPORTANT: share these links only through a secure channel\n"
            "(e.g. Signal, encrypted email).  Anyone with the link can join\n"
            "the broadcast channel and receive forwarded messages."
        )
        print()

    _info(
        "Log files\n"
        "---------\n"
        "Logs are written to the logs/ directory inside the install folder.\n"
        "Rotation: 10 MB per file, 10 files kept (100 MB total)."
    )
    print()

    os_name = platform.system()
    if os_name == "Linux":
        _info(
            "Service control (Linux)\n"
            "-----------------------\n"
            "  Status : sudo systemctl status aardvark-relay\n"
            "  Start  : sudo systemctl start  aardvark-relay\n"
            "  Stop   : sudo systemctl stop   aardvark-relay\n"
            "  Restart: sudo systemctl restart aardvark-relay\n"
            "  Logs   : journalctl -u aardvark-relay -f\n"
            "           or: tail -f /opt/aardvark/logs/relay.log"
        )
    elif os_name == "Darwin":
        _info(
            "Service control (macOS)\n"
            "-----------------------\n"
            "  Status : launchctl print gui/$(id -u)/com.aardvark.relay\n"
            '  Stop   : launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aardvark.relay.plist\n'
            '  Start  : launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aardvark.relay.plist\n'
            '  Logs   : tail -f "$HOME/Library/Application Support/Aardvark/logs/relay.log"'
        )
    elif os_name == "Windows":
        _info(
            "Service control (Windows)\n"
            "-------------------------\n"
            "  Status : sc query AardvarkRelay\n"
            "  Start  : sc start  AardvarkRelay\n"
            "  Stop   : sc stop   AardvarkRelay\n"
            "  Logs   : C:\\Program Files\\Aardvark\\logs\\relay.log"
        )
    else:
        _info(
            "Run the service:\n"
            "  .venv/bin/python app/relay.py --config config.toml\n"
            "\n"
            "Logs are in the logs/ subdirectory."
        )

    print()
    _info(
        "Hot reload\n"
        "----------\n"
        "The service watches config.toml while running.\n"
        "Channel additions/removals and burst settings apply within ~30 seconds.\n"
        "Most other changes require a service restart."
    )
    print()
    _info(f"README: {(output.parent / 'README.md').resolve()}")
    print()
    _info(
        "First-time Telegram login\n"
        "-------------------------\n"
        "BEFORE starting the service, run this command once to authenticate\n"
        "interactively with Telegram (enter SMS code + Cloud Password if 2FA):\n"
        "\n"
        "  Linux/macOS: .venv/bin/python app/relay.py --login --config config.toml\n"
        "  Windows    : .venv\\Scripts\\python app\\relay.py --login --config config.toml\n"
        "\n"
        "The session is saved after one successful login and reused automatically."
    )
    print()
    _info(
        "Proxy notes\n"
        "-----------\n"
        "[proxy]    in config.toml = Telegram proxy (mtproto / socks5 / http)\n"
        "[dc_proxy] in config.toml = Delta Chat + email proxy (socks5/http ONLY)\n"
        "MTProto is Telegram-specific and CANNOT be used for DC or email.\n"
        "If Telegram uses MTProto and DC/email also need a proxy, add [dc_proxy]\n"
        "with type=socks5 to config.toml.  See config_example.toml for examples."
    )
    _hr()


# ---------------------------------------------------------------------------
# Wizard sections
# ---------------------------------------------------------------------------

def _wizard_telegram(tg: Dict[str, Any]) -> Dict[str, Any]:
    _section("Step 1 of 7  \u2013  Telegram credentials")
    _info(
        "Aardvark uses your personal Telegram account to read channels.\n"
        "\n"
        "API ID and API Hash\n"
        "-------------------\n"
        "These identify the Aardvark application to Telegram servers.\n"
        "\n"
        "How to get them (one-time setup):\n"
        "  1. Open https://my.telegram.org/apps in a browser\n"
        "  2. Sign in with your Telegram phone number\n"
        "  3. Click 'API development tools'\n"
        "  4. Create a new application (any name, e.g. 'Aardvark relay')\n"
        "  5. Copy:\n"
        "       api_id   \u2192 a number, e.g. 12345678\n"
        "       api_hash \u2192 a 32-character hex string, e.g. 0a1b2c3d...\n"
        "\n"
        "These values are private. Never share them.\n"
        "\n"
        "Phone number\n"
        "------------\n"
        "Your Telegram phone number in international format.\n"
        "Example: +12025551234  (include + and country code)\n"
        "\n"
        "Session name\n"
        "------------\n"
        "Name for the saved session file (no extension).\n"
        "Default: 'aardvark'  \u2192 saves as aardvark.session\n"
        "Change only if you run multiple Aardvark instances."
    )
    print()
    api_id = int(_ask("Telegram API ID", str(tg.get("api_id", "")) or None, _v_positive_int))
    api_hash = _ask("Telegram API hash (32 hex chars)", str(tg.get("api_hash", "")) or None, _v_api_hash)
    phone = _ask("Telegram phone number (e.g. +12025551234)", str(tg.get("phone", "")) or None, _v_phone)
    session_name = _ask("Session name", str(tg.get("session_name", "aardvark")), _v_nonempty)

    print()
    _info(
        "First login note\n"
        "----------------\n"
        "After this wizard finishes, run once:\n"
        "  .venv/bin/python app/relay.py --config config.toml --login\n"
        "Telegram sends an SMS code to your phone; enter it when prompted.\n"
        "If 2FA is enabled, also enter your Cloud Password.\n"
        "The session is then saved and reused automatically."
    )
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "session_name": session_name,
    }


def _wizard_channels(ch: Dict[str, Any]) -> Dict[str, Any]:
    _section("Step 2 of 7  \u2013  Telegram channels to monitor")
    existing = [str(v) for v in ch.get("watch", [])]
    watch = _ask_channels(existing)
    return {"watch": watch}


def _wizard_relay_mode() -> str:
    _section("Step 3 of 7  \u2013  Relay destination")
    _info(
        "Choose where to forward Telegram messages:\n"
        "\n"
        "  dc    \u2013 Delta Chat broadcast channels only  (default)\n"
        "  email \u2013 plain email only\n"
        "  both  \u2013 Delta Chat AND email\n"
        "\n"
        "Delta Chat is an encrypted messenger built on email.\n"
        "Recipients download the free Delta Chat app and join your broadcast\n"
        "channels via an invite link.  Messages appear like a chat.\n"
        "Advantages: end-to-end encrypted, no phone number needed.\n"
        "\n"
        "Plain email sends to standard email addresses via SMTP.\n"
        "No app needed \u2013 messages arrive as regular emails.\n"
        "\n"
        "Both modes are independent and can run simultaneously.\n"
        "\n"
        "Press Enter to accept the default [dc]."
    )
    print()
    while True:
        choice = input("Relay mode (dc / email / both) [dc]: ").strip().lower() or "dc"
        if choice in {"dc", "email", "both"}:
            return choice
        print("  \u2717 Enter dc, email, or both.")


def _wizard_delta_chat(dc: Dict[str, Any]) -> Dict[str, Any]:
    _section("Step 4a of 7  \u2013  Delta Chat email account")
    _info(
        "Delta Chat uses email as its transport (IMAP + SMTP).\n"
        "Aardvark needs a dedicated sender email account.\n"
        "\n"
        "IMPORTANT: use a separate, dedicated email address.\n"
        "Do not use your personal inbox.\n"
        "Suggested: Fastmail, Mailbox.org, or any IMAP/SMTP provider.\n"
        "\n"
        "Password / App Password\n"
        "-----------------------\n"
        "Some providers require an application-specific password:\n"
        "  Gmail       \u2192 App Password required when 2FA is on\n"
        "              (Google Account \u2192 Security \u2192 App passwords)\n"
        "  Yandex      \u2192 enable IMAP in mail settings first;\n"
        "              use account password or App Password if 2FA is on\n"
        "  Outlook     \u2192 App Password if 2FA is enabled\n"
        "  Fastmail, Mailbox.org \u2192 regular password works\n"
        "\n"
        "IMAP and SMTP server hostnames\n"
        "------------------------------\n"
        "Usually auto-detected.  Enter only if auto-detect fails.\n"
        "Common values:\n"
        "  Gmail:          imap.gmail.com   /  smtp.gmail.com\n"
        "  Yandex:         imap.yandex.ru   /  smtp.yandex.ru\n"
        "  Fastmail:       imap.fastmail.com/  smtp.fastmail.com\n"
        "  Mailbox.org:    imap.mailbox.org /  smtp.mailbox.org\n"
        "  Outlook/Hotmail:outlook.office365.com (both IMAP and SMTP)"
    )
    print()
    addr = _ask("Sender email address", str(dc.get("addr", "")) or None, _v_email)
    mail_pw = _ask("Email password or App Password", None, _v_nonempty, secret=True)
    database_path = _ask(
        "Delta Chat database filename",
        str(dc.get("database_path", "deltachat.db")), _v_nonempty,
    )
    print()
    _info(
        "IMAP / SMTP overrides  (press Enter to skip and use auto-detect)"
    )
    mail_server = _ask(
        "IMAP server hostname (blank = auto-detect)",
        str(dc.get("mail_server", "")) or None, allow_empty=True,
    )
    send_server = _ask(
        "SMTP server hostname (blank = auto-detect)",
        str(dc.get("send_server", "")) or None, allow_empty=True,
    )
    return {
        "enabled": True,
        "addr": addr,
        "mail_pw": mail_pw,
        "database_path": database_path,
        "mail_server": mail_server or "",
        "send_server": send_server or "",
    }


def _wizard_email_relay(em: Dict[str, Any], dc_addr: str = "") -> Dict[str, Any]:
    _section("Step 4b of 7  \u2013  Plain email relay")
    _info(
        "Forwards Telegram messages to regular email addresses via SMTP.\n"
        "\n"
        "IMPORTANT: use a dedicated sender address, not your personal inbox.\n"
        "\n"
        "SSL mode:\n"
        "  ssl      \u2013 implicit TLS, port 465  (recommended)\n"
        "  starttls \u2013 STARTTLS upgrade, port 587\n"
        "  none     \u2013 plain (only for local/trusted servers)\n"
        "\n"
        "Password: same rules as Delta Chat \u2013 use App Password if 2FA is on."
    )
    if dc_addr:
        _info(f"\nYou may reuse the Delta Chat address: {dc_addr}")
    print()

    smtp_host = _ask("SMTP server hostname", str(em.get("smtp_host", "")) or None, _v_nonempty)
    ssl_mode = _ask("SSL mode", str(em.get("ssl_mode", "ssl")), _v_ssl_mode)
    default_port = "465" if ssl_mode == "ssl" else ("587" if ssl_mode == "starttls" else "25")
    smtp_port = int(_ask("SMTP port", str(em.get("smtp_port", default_port)), _v_port))
    smtp_user = _ask(
        "Sender email address (SMTP login)",
        str(em.get("smtp_user", dc_addr)) or None, _v_email,
    )
    smtp_password = _ask("SMTP password or App Password", None, _v_nonempty, secret=True)
    from_name = _ask("Sender display name", str(em.get("from_name", "Aardvark")), _v_nonempty)
    existing_targets = [str(v) for v in em.get("target_emails", [])]
    if em.get("target_email"):
        existing_targets.append(str(em["target_email"]))
    target_emails = _ask_emails(
        "Recipient email addresses (comma-separated)", existing_targets, required=True
    )
    return {
        "enabled": True,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "ssl_mode": ssl_mode,
        "target_emails": target_emails,
        "from_name": from_name,
        "use_tls": False,
    }


def _wizard_proxy(
    pr: Dict[str, Any],
    dc_pr: Dict[str, Any],
    relay_mode: str,
    imap_host: str = "",
    smtp_host: str = "",
) -> Dict[str, Any]:
    """
    Tests direct connectivity first, then optionally configures proxies.
    Returns a dict with keys 'proxy' and optionally 'dc_proxy'.
    """
    _section("Step 5 of 7  \u2013  Network connectivity and proxy (optional)")

    _info(
        "Aardvark will now test whether Telegram and your email servers are\n"
        "reachable directly (without a proxy).\n"
        "\n"
        "If all connections show REACHABLE, you likely do not need a proxy.\n"
        "If some fail, a proxy or VPN may be required.\n"
        "Tip: if you just enabled a VPN, you can re-test after connecting."
    )

    # --- Direct connectivity check with re-test loop ---
    tg_direct_ok, imap_ok, smtp_ok = _connectivity_check_loop(
        imap_host=imap_host if relay_mode in {"dc", "both"} else "",
        smtp_host=smtp_host if relay_mode in {"email", "both"} else "",
    )

    if tg_direct_ok and (imap_ok is None or imap_ok) and (smtp_ok is None or smtp_ok):
        _info("All connections reachable directly. A proxy is probably not needed.")
    else:
        _info("Some connections failed. A proxy may help.")

    _info(
        "\nPROXY ARCHITECTURE\n"
        "==================\n"
        "  [proxy]    \u2013 for Telegram ONLY  (socks5 / http / mtproto)\n"
        "  [dc_proxy] \u2013 for Delta Chat and email ONLY  (socks5 / http)\n"
        "\n"
        "Common setups:\n"
        "  A) MTProto for Telegram + SOCKS5 for DC/email:\n"
        "       [proxy] type=mtproto   +   [dc_proxy] type=socks5\n"
        "  B) Same SOCKS5 for everything (e.g. Karing / Clash on localhost):\n"
        "       [proxy] type=socks5, use_for_dc=true  (no [dc_proxy] needed)\n"
        "  C) Only Telegram needs a proxy:\n"
        "       [proxy] type=socks5, use_for_dc=false"
    )
    print()

    _no_proxy_result: Dict[str, Any] = {
        "proxy": {
            "enabled": False, "type": "socks5", "host": "", "port": 1080,
            "username": "", "password": "", "rdns": True,
            "use_for_dc": False, "use_for_email": False,
        },
        "dc_proxy": None,
    }

    if not _ask_yn("Enable proxy for Telegram?", default=bool(pr.get("enabled", False))):
        return _no_proxy_result

    # --- Telegram proxy details ---
    while True:
        _info(
            "Proxy type:\n"
            "  socks5  \u2013 SOCKS5 proxy (also works for DC/email when shared)\n"
            "  http    \u2013 HTTP CONNECT proxy (also works for DC/email)\n"
            "  mtproto \u2013 Telegram MTProto proxy (Telegram only; no extra packages)"
        )
        ptype = _ask("Telegram proxy type", str(pr.get("type", "socks5")), _v_proxy_type)
        host  = _ask("Proxy host or IP address", str(pr.get("host", "")) or None, _v_nonempty)
        default_port = "443" if ptype == "mtproto" else "1080"
        port  = int(_ask(f"Proxy port", str(pr.get("port", default_port)), _v_port))

        if ptype == "mtproto":
            _info(
                "MTProto secret\n"
                "--------------\n"
                "A long hex or base64 string provided by the proxy operator.\n"
                "Leave username blank for MTProto proxies."
            )
            password = _ask("Proxy secret (hex or base64)", str(pr.get("password", "")) or None, _v_nonempty)
            username = ""
        else:
            username = _ask("Proxy username (Enter if none)", str(pr.get("username", "")), allow_empty=True)
            password = _ask("Proxy password (Enter if none)", str(pr.get("password", "")), allow_empty=True)

        rdns = True if ptype == "mtproto" else _ask_yn("Route DNS through proxy? (recommended)", True)

        proxy_ok = _show_proxy_check(host, port)
        if proxy_ok:
            break
        if not _ask_yn("Proxy is unreachable. Re-enter proxy details?", default=True):
            _warn("Continuing. Edit config.toml later to fix the proxy.")
            break

    tg_proxy: Dict[str, Any] = {
        "enabled": True, "type": ptype, "host": host, "port": port,
        "username": username, "password": password, "rdns": rdns,
        "use_for_dc": False, "use_for_email": False,
    }

    # --- DC / email proxy ---
    dc_proxy_result: Optional[Dict[str, Any]] = None

    if ptype == "mtproto":
        print()
        _info(
            "MTProto is Telegram-only.  Delta Chat and email relay cannot use\n"
            "MTProto.  Configure a separate SOCKS5/HTTP proxy for DC/email if needed."
        )
        needs_dc_proxy = False
        if relay_mode in {"dc", "both"}:
            needs_dc_proxy = _ask_yn(
                "Does Delta Chat also need a SOCKS5/HTTP proxy?",
                default=bool(dc_pr.get("enabled", False)),
            )
        if not needs_dc_proxy and relay_mode in {"email", "both"}:
            needs_dc_proxy = _ask_yn(
                "Does the email relay also need a SOCKS5/HTTP proxy?",
                default=bool(dc_pr.get("enabled", False)),
            )

        if needs_dc_proxy:
            while True:
                dc_ptype = _ask(
                    "Proxy type for DC/email (socks5 or http)",
                    str(dc_pr.get("type", "socks5")),
                    lambda v: None if v in {"socks5", "http"} else "enter socks5 or http",
                )
                dc_host = _ask("Proxy host for DC/email", str(dc_pr.get("host", "")) or None, _v_nonempty)
                dc_port = int(_ask("Proxy port", str(dc_pr.get("port", 1080)), _v_port))
                dc_user = _ask("Proxy username (Enter if none)", str(dc_pr.get("username", "")), allow_empty=True)
                dc_pass = _ask("Proxy password (Enter if none)", str(dc_pr.get("password", "")), allow_empty=True)
                dc_rdns = _ask_yn("Route DNS through DC proxy?", True)

                dc_proxy_ok = _show_proxy_check(dc_host, dc_port)
                if dc_proxy_ok:
                    break
                if not _ask_yn("DC proxy unreachable. Re-enter?", default=True):
                    _warn("Continuing. Edit config.toml later to fix the DC proxy.")
                    break

            dc_proxy_result = {
                "enabled": True, "type": dc_ptype,
                "host": dc_host, "port": dc_port,
                "username": dc_user, "password": dc_pass, "rdns": dc_rdns,
                "use_for_dc": relay_mode in {"dc", "both"},
                "use_for_email": relay_mode in {"email", "both"},
            }
    else:
        # SOCKS5/HTTP: ask if DC/email should share it
        if relay_mode in {"dc", "both"}:
            tg_proxy["use_for_dc"] = _ask_yn("Also use this proxy for Delta Chat connections?", True)
        if relay_mode in {"email", "both"}:
            tg_proxy["use_for_email"] = _ask_yn("Also use this proxy for email relay connections?", True)

    return {"proxy": tg_proxy, "dc_proxy": dc_proxy_result}


def _wizard_relay_settings(rel: Dict[str, Any]) -> Dict[str, Any]:
    _section("Step 6 of 7  \u2013  Relay settings")
    _info(
        "history_mode  \u2013  what to replay when the service starts:\n"
        "  last_n       \u2013 the last N messages (default: 3)\n"
        "  since_today  \u2013 all messages since midnight UTC today\n"
        "\n"
        "max_media_size_mb  \u2013  files larger than this are skipped and replaced\n"
        "  by a placeholder text.  Default: 10 MB.  0 = relay all sizes.\n"
        "\n"
        "Press Enter on each field to accept the default shown in [brackets]."
    )
    print()
    history_mode = _ask(
        "History mode",
        str(rel.get("history_mode", "last_n")),
        lambda v: None if v in {"last_n", "since_today"} else "enter last_n or since_today",
    )
    history_last_n = int(_ask(
        "Messages to replay on first start",
        str(rel.get("history_last_n", 3)), _v_positive_int,
    ))
    max_media = float(_ask(
        "Max media size in MB (0 = all sizes)",
        str(rel.get("max_media_size_mb", 10)), _v_float_ge0,
    ))
    return {
        "history_mode": history_mode,
        "history_last_n": history_last_n,
        "max_media_size_mb": max_media,
        "invite_links_file": str(rel.get("invite_links_file", "invite_links.txt")),
        "state_file": str(rel.get("state_file", "relay_state.json")),
        "album_mode": str(rel.get("album_mode", "all_files")),
        "album_window_seconds": float(rel.get("album_window_seconds", 5.0)),
    }


def _wizard_burst(burst: Dict[str, Any]) -> Dict[str, Any]:
    _section("Step 7 of 7  \u2013  Burst limiter")
    _info(
        "Prevents rapid-fire text posts from flooding your inbox.\n"
        "\n"
        "When >= threshold messages arrive within window_seconds, they are\n"
        "buffered and combined into one message after the window expires.\n"
        "Media messages always bypass the limiter.\n"
        "\n"
        "Defaults: 20 messages / 300 seconds (5 minutes).\n"
        "Press Enter three times to accept all defaults."
    )
    print()
    if not _ask_yn("Enable burst limiter?", default=bool(burst.get("enabled", True))):
        return {"enabled": False, "threshold": 20, "window_seconds": 300}

    threshold = int(_ask(
        "Burst threshold (messages before buffering)",
        str(burst.get("threshold", 20)), _v_positive_int,
    ))
    window = int(_ask(
        "Window in seconds (quiet timeout before flush)",
        str(burst.get("window_seconds", 300)), _v_positive_int,
    ))
    return {"enabled": True, "threshold": threshold, "window_seconds": window}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Aardvark configuration wizard")
    parser.add_argument(
        "--output", default="config.toml",
        help="Path to config.toml to write (default: config.toml)",
    )
    parser.add_argument(
        "--install-dir", default=None,
        help="Install directory path (used to resolve absolute paths)",
    )
    args = parser.parse_args()
    output = Path(args.output)
    existing = _load_existing(output)

    _hr("=")
    print("  Aardvark setup wizard")
    _hr("=")
    print()
    _info(
        "This wizard creates or updates config.toml.\n"
        "\n"
        "For every field, the default is shown in [brackets].\n"
        "Press Enter to accept a default without retyping it.\n"
        "Passwords are hidden while you type.\n"
        "\n"
        "Prepare the following before starting:\n"
        "  \u2713 Telegram API ID and API hash  (from my.telegram.org/apps)\n"
        "  \u2713 Telegram phone number         (international format, e.g. +12025551234)\n"
        "  \u2713 Dedicated sender email address (not your personal inbox)\n"
        "  \u2713 Email password or App Password (required by some providers with 2FA)\n"
        "  \u2713 IMAP server hostname           (e.g. imap.gmail.com)\n"
        "  \u2713 SMTP server hostname           (e.g. smtp.gmail.com)\n"
        "  \u2713 Telegram channel names         (@username or t.me/link)"
    )
    if output.exists():
        print()
        _info(f"Existing config found: {output.resolve()}")
        _info("Existing values will be shown as defaults.")

    tg_data = _wizard_telegram(existing.get("telegram", {}))
    ch_data = _wizard_channels(existing.get("channels", {}))
    relay_mode = _wizard_relay_mode()

    dc_existing = existing.get("delta_chat", {})
    if relay_mode in {"dc", "both"}:
        dc_data = _wizard_delta_chat(dc_existing)
    else:
        dc_data = {
            "enabled": False, "addr": "", "mail_pw": "",
            "database_path": "deltachat.db", "mail_server": "", "send_server": "",
        }

    em_existing = existing.get("email_relay", {})
    if relay_mode in {"email", "both"}:
        em_data = _wizard_email_relay(em_existing, dc_addr=dc_data.get("addr", ""))
    else:
        em_data = {
            "enabled": False, "smtp_host": "", "smtp_port": 465,
            "smtp_user": "", "smtp_password": "", "ssl_mode": "ssl",
            "target_emails": [], "from_name": "Aardvark", "use_tls": False,
        }

    # Use configured mail/smtp hosts for connectivity check in the proxy step
    imap_host = dc_data.get("mail_server", "") or ""
    smtp_host  = em_data.get("smtp_host", "") or ""

    pr_result = _wizard_proxy(
        existing.get("proxy", {}),
        existing.get("dc_proxy", {}),
        relay_mode,
        imap_host=imap_host,
        smtp_host=smtp_host,
    )
    pr_data    = pr_result["proxy"]
    dc_pr_data = pr_result.get("dc_proxy")
    rel_data   = _wizard_relay_settings(existing.get("relay", {}))
    burst_data = _wizard_burst(existing.get("burst", {}))

    data = {
        "telegram": tg_data,
        "channels": ch_data,
        "delta_chat": dc_data,
        "relay": rel_data,
        "burst": burst_data,
        "proxy": pr_data,
        "dc_proxy": dc_pr_data,
        "email_relay": em_data,
    }

    _write_config(output, data, install_dir=args.install_dir)
    print()
    _info(f"\u2713 Config written to {output.resolve()}")

    _print_post_setup(output, args.install_dir, relay_mode)


if __name__ == "__main__":
    main()
