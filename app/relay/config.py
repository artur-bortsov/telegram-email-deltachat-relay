"""
Configuration loading and validation.

All settings are read from a TOML file (default: config.toml).
Python 3.11+ has tomllib built-in; older versions need the 'tomli' package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    import tomllib                       # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib          # type: ignore[no-redef]
    except ImportError as exc:
        raise ImportError(
            "tomli is required for Python < 3.11.  "
            "Install it with:  pip install tomli"
        ) from exc


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TelegramConfig:
    """Credentials and session settings for the Telegram MTProto client."""
    api_id: int
    api_hash: str
    phone: str
    session_name: str = "aardvark"


@dataclass
class ChannelsConfig:
    """List of Telegram channels to monitor."""
    watch: List[str] = field(default_factory=list)


@dataclass
class DeltaChatConfig:
    """Email account used by the Delta Chat client."""
    # Set enabled = false to skip Delta Chat entirely and use email relay only.
    enabled: bool = True
    addr: str = ""
    mail_pw: str = ""
    database_path: str = "deltachat.db"
    # Override auto-detected IMAP / SMTP server hostnames when needed.
    mail_server: Optional[str] = None
    send_server: Optional[str] = None


@dataclass
class RelayConfig:
    """General relay behaviour settings."""
    # "last_n"      -> relay the last N messages on startup
    # "since_today" -> relay all messages posted since midnight UTC today
    history_mode: str = "last_n"
    history_last_n: int = 3
    invite_links_file: str = "invite_links.txt"
    # Automatically create DC channels named after Telegram channels.
    auto_create: bool = True
    # Media files larger than this (in MB) are replaced by a placeholder text.
    # Set to 0.0 to relay all media regardless of size.
    max_media_size_mb: float = 10.0
    # File that persists the last-relayed message ID per channel.
    # Prevents duplicate messages on service restart.
    state_file: str = "relay_state.json"
    # How long (seconds) to wait for all parts of a Telegram media album before
    # relaying.  Increase on slow or high-latency connections.
    album_window_seconds: float = 5.0
    # How to relay Telegram media albums (multiple photos/videos in one post):
    #   "all_files"  - first DC message: combined caption + first media file;
    #                  remaining files follow as separate media-only messages
    #   "first_only" - one DC message only: first media + caption +
    #                  "[+N more]" note (skips remaining files)
    album_mode: str = "all_files"


@dataclass
class BurstConfig:
    """Burst-limiter settings."""
    enabled: bool = True
    threshold: int = 20            # messages in the window before combining
    window_seconds: int = 300      # sliding window length (default: 5 min)
    separator: str = "\n---\n"     # text inserted between combined messages


@dataclass
class ProxyConfig:
    """
    Proxy settings for outgoing connections.

    Telegram supports SOCKS5, HTTP, and MTProto proxies.
    Delta Chat and email relay support SOCKS5 only (MTProto is Telegram-native).
    """
    enabled: bool = False
    # Proxy type: "socks5", "http", or "mtproto"
    type: str = "socks5"
    host: str = ""
    port: int = 1080
    # Optional username / password (leave empty for unauthenticated proxies).
    # For MTProto proxies: leave username empty; put the proxy secret
    # (hex or base64 string) in the password field.
    username: str = ""
    password: str = ""
    # Route DNS lookups through the proxy (SOCKS5 and HTTP only).
    rdns: bool = True
    # Also use this proxy for Delta Chat connections (ignored for MTProto).
    use_for_dc: bool = True
    # Also use this proxy for email relay connections (ignored for MTProto).
    use_for_email: bool = True


@dataclass
class EmailRelayConfig:
    """Optional plain-SMTP relay (independent of Delta Chat)."""
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    # SSL mode for SMTP connections:
    #   "ssl"      -> implicit TLS (SMTP_SSL), recommended, typically port 465
    #   "starttls" -> STARTTLS upgrade, typically port 587
    #   "none"     -> plain unencrypted (strongly discouraged outside localhost)
    ssl_mode: str = "ssl"
    # List of recipient addresses; also accepts legacy single target_email.
    target_emails: List[str] = field(default_factory=list)
    from_name: str = "Aardvark"
    # Deprecated: use ssl_mode instead.  Kept for backward compatibility.
    # use_tls = true maps to ssl_mode = "starttls".
    use_tls: bool = False


@dataclass
class Config:
    """Root configuration object."""
    telegram: TelegramConfig
    channels: ChannelsConfig
    delta_chat: Optional[DeltaChatConfig]   # None when credentials absent and not explicitly disabled
    relay: RelayConfig = field(default_factory=RelayConfig)
    burst: BurstConfig = field(default_factory=BurstConfig)
    email_relay: EmailRelayConfig = field(default_factory=EmailRelayConfig)
    # proxy: Telegram proxy (supports socks5, http, mtproto)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    # dc_proxy: separate proxy for Delta Chat and email relay (socks5/http only).
    # Populated from [dc_proxy] in config.toml, or auto-inherited from [proxy]
    # when proxy.type is socks5/http and proxy.use_for_dc / use_for_email is true.
    # None means DC and email connect directly (no proxy).
    dc_proxy: Optional[ProxyConfig] = None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> Config:
    """Load and parse configuration from a TOML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Copy config_example.toml to config.toml and fill in your details."
        )

    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)

    # --- Telegram (required) ---
    tg = raw.get("telegram", {})
    if not tg.get("api_id") or not tg.get("api_hash") or not tg.get("phone"):
        raise ValueError(
            "[telegram] section must contain api_id, api_hash, and phone."
        )
    telegram = TelegramConfig(
        api_id=int(tg["api_id"]),
        api_hash=str(tg["api_hash"]),
        phone=str(tg["phone"]),
        session_name=str(tg.get("session_name", "aardvark")),
    )

    # --- Channels ---
    ch = raw.get("channels", {})
    channels = ChannelsConfig(watch=[str(c) for c in ch.get("watch", [])])

    # --- Delta Chat (optional) ---
    dc = raw.get("delta_chat", {})
    dc_explicitly_disabled = "enabled" in dc and not bool(dc["enabled"])
    delta_chat: Optional[DeltaChatConfig] = None
    if dc_explicitly_disabled:
        # Create a stub so relay.py can log the appropriate message.
        delta_chat = DeltaChatConfig(enabled=False)
    elif dc.get("addr") and dc.get("mail_pw"):
        delta_chat = DeltaChatConfig(
            enabled=True,
            addr=str(dc["addr"]),
            mail_pw=str(dc["mail_pw"]),
            database_path=str(dc.get("database_path", "deltachat.db")),
            mail_server=dc.get("mail_server") or None,
            send_server=dc.get("send_server") or None,
        )
    # else: delta_chat stays None -> DC disabled, relay.py will log a warning

    # --- Relay settings ---
    rel = raw.get("relay", {})
    relay = RelayConfig(
        history_mode=str(rel.get("history_mode", "last_n")),
        history_last_n=int(rel.get("history_last_n", 3)),
        invite_links_file=str(rel.get("invite_links_file", "invite_links.txt")),
        auto_create=bool(rel.get("auto_create", True)),
        max_media_size_mb=float(rel.get("max_media_size_mb", 10.0)),
        state_file=str(rel.get("state_file", "relay_state.json")),
        album_window_seconds=float(rel.get("album_window_seconds", 5.0)),
        album_mode=str(rel.get("album_mode", "all_files")),
    )
    if relay.history_mode not in {"last_n", "since_today"}:
        raise ValueError(
            f"relay.history_mode must be 'last_n' or 'since_today', "
            f"got: {relay.history_mode!r}"
        )
    if relay.album_mode not in {"first_only", "all_files"}:
        raise ValueError(
            f"relay.album_mode must be 'first_only' or 'all_files', "
            f"got: {relay.album_mode!r}"
        )

    # --- Burst limiter ---
    burst_raw = raw.get("burst", {})
    burst = BurstConfig(
        enabled=bool(burst_raw.get("enabled", True)),
        threshold=int(burst_raw.get("threshold", 20)),
        window_seconds=int(burst_raw.get("window_seconds", 300)),
        separator=str(burst_raw.get("separator", "\n---\n")),
    )

    # --- Proxy ---
    proxy_raw = raw.get("proxy", {})
    proxy = ProxyConfig(
        enabled=bool(proxy_raw.get("enabled", False)),
        type=str(proxy_raw.get("type", "socks5")),
        host=str(proxy_raw.get("host", "")),
        port=int(proxy_raw.get("port", 1080)),
        username=str(proxy_raw.get("username", "")),
        password=str(proxy_raw.get("password", "")),
        rdns=bool(proxy_raw.get("rdns", True)),
        use_for_dc=bool(proxy_raw.get("use_for_dc", True)),
        use_for_email=bool(proxy_raw.get("use_for_email", True)),
    )
    if proxy.enabled:
        if proxy.type not in {"socks5", "http", "mtproto"}:
            raise ValueError(
                f"proxy.type must be 'socks5', 'http', or 'mtproto', "
                f"got: {proxy.type!r}"
            )
        if not proxy.host:
            raise ValueError("proxy.host is required when proxy.enabled = true")

    # --- Email relay ---
    em = raw.get("email_relay", {})
    # ssl_mode takes priority; fall back to legacy use_tls if present.
    if em.get("ssl_mode"):
        ssl_mode = str(em["ssl_mode"])
    elif "use_tls" in em:
        ssl_mode = "starttls" if bool(em["use_tls"]) else "ssl"
    else:
        ssl_mode = "ssl"
    if ssl_mode not in {"ssl", "starttls", "none"}:
        raise ValueError(
            f"email_relay.ssl_mode must be 'ssl', 'starttls', or 'none', "
            f"got: {ssl_mode!r}"
        )
    # Support both target_emails (list) and legacy target_email (single string).
    raw_emails: List[str] = [str(a) for a in em.get("target_emails", [])]
    if em.get("target_email"):                          # legacy fallback
        legacy = str(em["target_email"])
        if legacy not in raw_emails:
            raw_emails.append(legacy)
    email_relay = EmailRelayConfig(
        enabled=bool(em.get("enabled", False)),
        smtp_host=str(em.get("smtp_host", "")),
        smtp_port=int(em.get("smtp_port", 465)),
        smtp_user=str(em.get("smtp_user", "")),
        smtp_password=str(em.get("smtp_password", "")),
        ssl_mode=ssl_mode,
        target_emails=raw_emails,
        from_name=str(em.get("from_name", "Aardvark")),
    )
    if email_relay.enabled and not email_relay.target_emails:
        raise ValueError(
            "email_relay is enabled but target_emails is empty."
        )

    # --- DC / email proxy ---
    # Explicit [dc_proxy] section takes precedence.  If absent, auto-inherit
    # from [proxy] when type is socks5/http (MTProto is Telegram-only).
    dc_proxy_raw = raw.get("dc_proxy", {})
    dc_proxy: Optional[ProxyConfig] = None
    if dc_proxy_raw.get("enabled"):
        dc_type = str(dc_proxy_raw.get("type", "socks5"))
        if dc_type not in {"socks5", "http"}:
            raise ValueError(
                f"dc_proxy.type must be 'socks5' or 'http', got: {dc_type!r}"
            )
        if not dc_proxy_raw.get("host"):
            raise ValueError("dc_proxy.host is required when dc_proxy.enabled = true")
        dc_proxy = ProxyConfig(
            enabled=True,
            type=dc_type,
            host=str(dc_proxy_raw["host"]),
            port=int(dc_proxy_raw.get("port", 1080)),
            username=str(dc_proxy_raw.get("username", "")),
            password=str(dc_proxy_raw.get("password", "")),
            rdns=bool(dc_proxy_raw.get("rdns", True)),
            use_for_dc=bool(dc_proxy_raw.get("use_for_dc", True)),
            use_for_email=bool(dc_proxy_raw.get("use_for_email", True)),
        )
    elif proxy.enabled and proxy.type in {"socks5", "http"} and (proxy.use_for_dc or proxy.use_for_email):
        # Auto-inherit from the Telegram proxy when it is SOCKS5/HTTP.
        dc_proxy = ProxyConfig(
            enabled=True,
            type=proxy.type,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
            rdns=proxy.rdns,
            use_for_dc=proxy.use_for_dc,
            use_for_email=proxy.use_for_email,
        )

    return Config(
        telegram=telegram,
        channels=channels,
        delta_chat=delta_chat,
        relay=relay,
        burst=burst,
        email_relay=email_relay,
        proxy=proxy,
        dc_proxy=dc_proxy,
    )
