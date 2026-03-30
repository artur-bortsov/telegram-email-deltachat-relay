#!/usr/bin/env python3
"""
Validate an Aardvark config.toml file.

Exit codes:
  0 = acceptable config
  1 = invalid / incomplete config
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def _valid_channel(value: str) -> bool:
    """Return True for a syntactically valid channel entry."""
    if re.fullmatch(r"@[\w\d_]{3,}", value):
        return True
    if value.startswith("t.me/") and len(value) > 5:
        return True
    if re.fullmatch(r"-?\d+", value):
        return True
    return False


def validate(path: Path, require_complete: bool) -> int:
    if not path.exists():
        return _fail(f"Missing config file: {path}")

    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        return _fail(f"Config is not valid TOML: {exc}")

    # --- Telegram (always required) ---
    telegram = data.get("telegram", {})
    if not telegram.get("api_id"):
        return _fail("telegram.api_id is missing or zero")
    if not telegram.get("api_hash"):
        return _fail("telegram.api_hash is missing")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", str(telegram.get("api_hash", ""))):
        _warn("telegram.api_hash does not look like a 32-character hex string")
    if not telegram.get("phone"):
        return _fail("telegram.phone is missing")

    # --- Channels (always required) ---
    channels = data.get("channels", {})
    watch = channels.get("watch", [])
    if not isinstance(watch, list) or not watch:
        return _fail("channels.watch must contain at least one channel")
    bad = [str(c) for c in watch if not _valid_channel(str(c))]
    if bad:
        return _fail(
            f"channels.watch contains invalid entries: {bad}  "
            "Use @username, t.me/username, or numeric ID."
        )

    if not require_complete:
        return 0

    # --- At least one relay output must be configured and enabled ---
    delta = data.get("delta_chat", {})
    dc_explicitly_disabled = "enabled" in delta and not bool(delta.get("enabled"))
    has_dc = (
        not dc_explicitly_disabled
        and bool(delta.get("addr"))
        and bool(delta.get("mail_pw"))
    )

    email = data.get("email_relay", {})
    has_email = bool(email.get("enabled")) and bool(
        email.get("smtp_host")
        and email.get("smtp_port")
        and email.get("smtp_user")
        and email.get("smtp_password")
        and (
            email.get("target_email")
            or (isinstance(email.get("target_emails"), list) and email.get("target_emails"))
        )
    )

    if not has_dc and not has_email:
        return _fail(
            "Config must enable at least one output relay.\n"
            "  Option A: set delta_chat.addr and delta_chat.mail_pw\n"
            "  Option B: set email_relay.enabled = true and fill in SMTP details"
        )

    # --- Proxy sanity check ---
    proxy = data.get("proxy", {})
    if proxy.get("enabled"):
        ptype = proxy.get("type", "")
        if ptype not in {"socks5", "http", "mtproto"}:
            return _fail(
                f"proxy.type must be 'socks5', 'http', or 'mtproto', got: {ptype!r}"
            )
        if not proxy.get("host"):
            return _fail("proxy.host is required when proxy.enabled = true")

    # --- Email SSL mode ---
    ssl_mode = email.get("ssl_mode", "")
    if ssl_mode and ssl_mode not in {"ssl", "starttls", "none"}:
        return _fail(
            f"email_relay.ssl_mode must be 'ssl', 'starttls', or 'none', got: {ssl_mode!r}"
        )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Aardvark config.toml")
    parser.add_argument("config_path", help="Path to config.toml")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Require full runtime-ready configuration (relay output configured)",
    )
    args = parser.parse_args()
    raise SystemExit(validate(Path(args.config_path), args.require_complete))


if __name__ == "__main__":
    main()
