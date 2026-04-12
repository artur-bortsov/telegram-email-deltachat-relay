"""
Delta Chat client: manages an email-based Delta Chat account, creates
one group chat per Telegram channel, sends forwarded messages, and
returns join (invite) links.

Delta Chat is email under the hood (IMAP + SMTP).  This module uses the
``deltachat-rpc-client`` package which communicates with a
``deltachat-rpc-server`` binary via JSON-RPC over stdio.  No C
compilation is needed; the server binary ships as a separate Python
wheel (``deltachat-rpc-server``).

All blocking RPC calls are wrapped in ``asyncio.run_in_executor`` so they
do not stall the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from .config import DeltaChatConfig, ProxyConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional imports: graceful degradation when packages are not installed
# ---------------------------------------------------------------------------
try:
    from deltachat_rpc_client import DeltaChat as _DeltaChat        # type: ignore[import]
    from deltachat_rpc_client.rpc import Rpc as _Rpc               # type: ignore[import]
    from deltachat_rpc_client.const import ChatType as _ChatType   # type: ignore[import]
    _DC_OK = True
except ImportError:
    _DeltaChat = None                # type: ignore[assignment]
    _Rpc = None                      # type: ignore[assignment]
    _DC_OK = False
    logger.warning(
        "deltachat-rpc-client not installed – Delta Chat forwarding disabled.\n"
        "Install with:  pip install deltachat-rpc-client deltachat-rpc-server"
    )


def _rpc_server_path() -> str:
    """Return the path to the deltachat-rpc-server binary.

    Prefers the binary bundled with the ``deltachat-rpc-server`` Python
    package; falls back to searching PATH.
    """
    try:
        import deltachat_rpc_server as _srv  # type: ignore[import]
        return os.path.join(os.path.dirname(_srv.__file__), "deltachat-rpc-server")
    except ImportError:
        return "deltachat-rpc-server"  # hope it is in PATH


class DeltaChatClient:
    """
    Wraps a single Delta Chat account and manages relay group chats.

    One group chat is created (or reused) per Telegram channel, named
    after that channel.  The account must be configured with valid email
    credentials before use; ``start()`` handles that automatically.
    """

    def __init__(
        self, cfg: DeltaChatConfig, proxy_cfg: Optional[ProxyConfig] = None
    ) -> None:
        if not _DC_OK:
            raise RuntimeError(
                "deltachat-rpc-client is not installed.\n"
                "Install with:  pip install deltachat-rpc-client deltachat-rpc-server"
            )
        self._cfg = cfg
        self._proxy_cfg = proxy_cfg
        self._rpc: Optional[_Rpc] = None
        self._dc: Optional[_DeltaChat] = None
        self._account: Optional[object] = None
        # Cache: channel name → Chat object
        self._chats: Dict[str, object] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the deltachat-rpc-server subprocess, open (or create) the
        account, configure it if needed, and start I/O.  Blocks until
        everything is ready.
        """
        # Derive an accounts directory from the database_path config value.
        # e.g. "deltachat.db" → "deltachat_accounts/"
        db_stem = Path(self._cfg.database_path).stem
        accounts_dir = str(
            Path(self._cfg.database_path).parent.resolve() / f"{db_stem}_accounts"
        )
        os.makedirs(accounts_dir, exist_ok=True)
        logger.info("Starting deltachat-rpc-server with accounts dir: %s", accounts_dir)

        self._rpc = _Rpc(
            accounts_dir=accounts_dir,
            rpc_server_path=_rpc_server_path(),
        )
        self._rpc.start()
        self._dc = _DeltaChat(self._rpc)

        # Retrieve the first existing account or create a new one
        accounts = self._dc.get_all_accounts()
        self._account = accounts[0] if accounts else self._dc.add_account()
        # Check whether configure() is actually needed.
        # is_configured() can return False after a crash (the flag was not
        # flushed to disk) even though all config values are intact.  Calling
        # configure() unnecessarily restarts the IMAP/SMTP stack, which can
        # crash the DC RPC server when the network or proxy is not fully ready.
        # Strategy: only call configure() when the stored addr differs from
        # what we want, or when no addr is stored at all.
        _needs_configure = True
        try:
            _stored_addr = self._account.get_config("addr") or ""
            if _stored_addr == self._cfg.addr and self._account.is_configured():
                # Address matches AND account is fully configured — safe to skip.
                _needs_configure = False
                logger.info(
                    "DC account has existing config for %s "
                    "(is_configured=True) - skipping configure(), calling start_io() directly.",
                    self._cfg.addr,
                )
            elif _stored_addr == self._cfg.addr:
                # Address is stored but is_configured=False: configure() previously
                # failed or the process crashed before the flag was flushed to disk.
                # Re-run configure() to restore the account to a working state.
                logger.info(
                    "DC account addr stored for %s but is_configured=False "
                    "-- re-running configure() to fix broken account state.",
                    self._cfg.addr,
                )
        except Exception:
            pass  # get_config failed → fall through to fresh configure

        if _needs_configure:
            logger.info("Configuring Delta Chat account for %s ...", self._cfg.addr)
            self._account.set_config("addr", self._cfg.addr)
            self._account.set_config("mail_pw", self._cfg.mail_pw)
            if self._cfg.mail_server:
                self._account.set_config("mail_server", self._cfg.mail_server)
            if self._cfg.send_server:
                self._account.set_config("send_server", self._cfg.send_server)
            # SocketSecurity enum: 0=Automatic, 1=SSL/TLS, 2=STARTTLS, 3=Plain
            # IMPORTANT: "3" is Plain (insecure), NOT TLS.  Use "1" for SSL.
            self._account.set_config("mail_security", "1")   # 1 = SSL/TLS  (port 993)
            self._account.set_config("send_security", "1")   # 1 = SSL/TLS  (port 465)
            # Apply SOCKS5 proxy if configured.
            self._apply_proxy()
            # configure() is a blocking call; may take 30-60 s on first run.
            # If it fails, stop the RPC subprocess immediately to prevent the
            # background events_loop thread from crashing the relay process.
            try:
                self._account.configure()
                logger.info("Delta Chat account configured")
            except Exception:
                logger.info("DC configure() failed; stopping RPC subprocess.")
                try:
                    self._rpc.close()
                except Exception:
                    pass
                raise

        self._account.start_io()
        logger.info("Delta Chat I/O started")

    @staticmethod
    def _tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
        """Return True when a TCP connection to host:port succeeds."""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            code = sock.connect_ex((host, port))
            sock.close()
            return code == 0
        except Exception:
            return False

    def _apply_proxy(self) -> None:
        """
        Configure SOCKS5 proxy for the Delta Chat account if applicable.
        MTProto is Telegram-only and ignored here.
        Skips the proxy silently if it is not reachable (prevents DC from
        hanging on a dead proxy for 30+ seconds).
        """
        cfg = self._proxy_cfg
        if cfg is None or not cfg.enabled or not cfg.use_for_dc:
            return
        if cfg.type not in {"socks5", "http"}:
            logger.info("DC proxy: type %r is not SOCKS5/HTTP - skipping", cfg.type)
            return
        # Connectivity check: don't give DC a proxy that can't be reached.
        if not self._tcp_reachable(cfg.host, cfg.port):
            logger.warning(
                "DC proxy %s:%d is not reachable - running DC without proxy.",
                cfg.host, cfg.port,
            )
            return
        logger.info("DC proxy reachable: SOCKS5 %s:%d", cfg.host, cfg.port)
        try:
            # Delta Chat uses integer socks5_port
            self._account.set_config("socks5_enabled", "1")
            self._account.set_config("socks5_host", cfg.host)
            self._account.set_config("socks5_port", str(cfg.port))
            if cfg.username:
                self._account.set_config("socks5_user", cfg.username)
            if cfg.password:
                self._account.set_config("socks5_password", cfg.password)
            logger.info("DC proxy: SOCKS5 %s:%d", cfg.host, cfg.port)
        except Exception:
            logger.exception("Failed to set SOCKS5 proxy on Delta Chat account")

    def stop(self) -> None:
        """Stop I/O and shut down the rpc-server subprocess."""
        if self._account is not None:
            try:
                self._account.stop_io()
            except Exception:
                pass
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
        logger.info("Delta Chat stopped")

    # ------------------------------------------------------------------
    # Chat management
    # ------------------------------------------------------------------

    def get_or_create_chat(self, channel_name: str) -> object:
        """
        Return the Delta Chat broadcast channel for *channel_name*, creating
        it if it does not exist yet.  Results are cached in memory.
        """
        if channel_name in self._chats:
            return self._chats[channel_name]

        existing = self._find_existing_chat(channel_name)
        if existing is not None:
            self._chats[channel_name] = existing
            logger.info("Reusing existing DC channel: %r", channel_name)
        else:
            # create_broadcast() makes an outgoing broadcast channel shown as
            # "Channel" in the Delta Chat UI – recipients get messages read-only.
            existing = self._account.create_broadcast(channel_name)
            self._chats[channel_name] = existing
            logger.info("Created new DC channel: %r", channel_name)

        # Ensure the chat is not stuck in contact-request state before sending.
        try:
            existing.accept()
        except Exception:
            pass  # already accepted – ignore

        return existing

    def get_invite_link(self, channel_name: str) -> Optional[str]:
        """
        Return the OPENPGP4FPR securejoin invite URL for the group chat.
        Returns *None* and logs a warning on any error.

        IMPORTANT: chat.get_qr_code() is a synchronous RPC call that can block
        indefinitely while DC performs its initial IMAP mailbox sync.  We run
        it in a separate thread with a 30-second timeout so the startup loop
        is never stuck waiting for a slow or temporarily overloaded mail server.
        The invite link will be retried on the next channel-message event.
        """
        try:
            chat = self.get_or_create_chat(channel_name)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(chat.get_qr_code)
                link = fut.result(timeout=30)   # 30 s max; DC may be syncing inbox
            return link
        except concurrent.futures.TimeoutError:
            logger.warning(
                "get_invite_link(%r) timed out after 30 s.  "
                "DC is likely still syncing the mailbox.  "
                "The invite link will be written once DC is ready.",
                channel_name,
            )
            return None
        except Exception:
            logger.exception("Failed to get invite link for chat %r", channel_name)
            return None

    async def update_channel_info_async(
        self,
        channel_name: str,
        photo_path: Optional[str] = None,
    ) -> None:
        """
        Update the DC channel's profile image from *photo_path*.
        Runs in a thread executor to avoid blocking the event loop.
        """
        if photo_path is None:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._update_channel_info_sync, channel_name, photo_path
        )

    def _update_channel_info_sync(
        self, channel_name: str, photo_path: Optional[str]
    ) -> None:
        """Blocking channel-info update; intended for thread executor."""
        try:
            chat = self.get_or_create_chat(channel_name)
            if photo_path and os.path.isfile(photo_path):
                snap = chat.get_basic_snapshot()
                current_path = snap.get("profile_image")
                if self._same_file_contents(current_path, photo_path):
                    logger.info(
                        "Profile photo unchanged for DC channel %r – skipping update",
                        channel_name,
                    )
                    return
                chat.set_image(photo_path)
                logger.info("Updated profile photo for DC channel %r", channel_name)
        except Exception:
            logger.exception("Failed to update channel info for %r", channel_name)

    @staticmethod
    def _same_file_contents(path_a: Optional[str], path_b: Optional[str]) -> bool:
        """
        Return True when both files exist and have identical byte content.
        Missing files are treated as non-equal.
        """
        if not path_a or not path_b:
            return False
        if not os.path.isfile(path_a) or not os.path.isfile(path_b):
            return False
        return DeltaChatClient._file_sha256(path_a) == DeltaChatClient._file_sha256(path_b)

    @staticmethod
    def _file_sha256(path: str) -> str:
        """Return SHA-256 hex digest for a file."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def get_all_broadcast_names(self) -> List[str]:
        """
        Return the names of all outgoing broadcast channels in the account.
        Used on startup to detect channels that are no longer in config.
        """
        from deltachat_rpc_client.const import ChatType as _CT  # avoid circular
        names: List[str] = []
        try:
            for chat in self._account.get_chatlist(no_specials=True):
                try:
                    snap = chat.get_basic_snapshot()
                    if snap.chat_type == _CT.OUT_BROADCAST:
                        names.append(snap.name)
                except Exception:
                    continue
        except Exception:
            logger.exception("Failed to list broadcast channel names")
        return names

    async def send_message(
        self,
        channel_name: str,
        text: str,
        media_path: Optional[str] = None,
    ) -> None:
        """
        Send a message (text, media, or both) to the DC channel for
        *channel_name*.  Runs the blocking call in a thread executor.
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_sync, channel_name, text, media_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_sync(
        self,
        channel_name: str,
        text: str,
        media_path: Optional[str] = None,
    ) -> None:
        """Blocking send; intended to run in a thread-pool executor."""
        # Defensive guard: never send an empty message to DC.
        # This should be prevented upstream, but belt-and-suspenders here
        # stops any empty string from becoming a blank DC message.
        if not text and not media_path:
            logger.warning(
                "DC send skipped for channel %r: empty text and no media.",
                channel_name,
            )
            return
        try:
            chat = self.get_or_create_chat(channel_name)
            if media_path:
                # Send file with optional caption in a single message
                chat.send_message(text=text or None, file=media_path)
            else:
                chat.send_text(text)
            logger.info(
                "Sent to DC channel %r: %s%s",
                channel_name,
                "[media] " if media_path else "",
                (text or "")[:80],
            )
        except Exception:
            logger.exception("Error sending to DC channel %r", channel_name)

    def _find_existing_chat(self, name: str) -> Optional[object]:
        """
        Search the account's chat list for an **outgoing broadcast channel**
        whose name matches *name*.  Only OUT_BROADCAST type is considered;
        group chats and contact-request chats are ignored.
        Returns the first match or *None*.

        Uses ``get_full_snapshot()`` because ``get_basic_snapshot()`` does
        not include the ``chattype`` field.
        """
        try:
            for chat in self._account.get_chatlist(no_specials=True):
                try:
                    snap = chat.get_basic_snapshot()
                    if snap.name == name and snap.chat_type == _ChatType.OUT_BROADCAST:
                        return chat
                except Exception:
                    continue
        except Exception:
            logger.exception("Error while searching for existing channel %r", name)
        return None
