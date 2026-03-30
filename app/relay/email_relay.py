"""
Plain e-mail relay: forwards messages via SMTP.

This is an *alternative* (or *addition*) to the Delta Chat relay and does
not require Delta Chat to be configured.  Messages are sent as plain-text
e-mails to the configured recipient addresses.

SSL mode is controlled by EmailRelayConfig.ssl_mode:
  "ssl"      – implicit TLS via SMTP_SSL (port 465, recommended)
  "starttls" – STARTTLS upgrade (port 587)
  "none"     – plain unencrypted (not recommended)
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import smtplib
import ssl
import socket
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from typing import Optional

from .config import EmailRelayConfig, ProxyConfig

# ---------------------------------------------------------------------------
# Optional PySocks import for email proxy support
# ---------------------------------------------------------------------------
try:
    import socks as _socks  # type: ignore[import]  # from PySocks
    _SOCKS_OK = True
except ImportError:
    _socks = None  # type: ignore[assignment]
    _SOCKS_OK = False

logger = logging.getLogger(__name__)


class EmailRelay:
    """Sends relay messages to target addresses via SMTP."""

    def __init__(
        self, cfg: EmailRelayConfig, proxy_cfg: Optional[ProxyConfig] = None
    ) -> None:
        self._cfg = cfg
        self._proxy_cfg = proxy_cfg

    async def send(
        self,
        subject: str,
        body: str,
        media_path: Optional[str] = None,
    ) -> None:
        """
        Send an e-mail asynchronously, with an optional media attachment.

        The blocking SMTP operations run in the default thread executor so
        the asyncio loop is not stalled.  Does nothing when the relay is
        disabled in configuration.
        """
        if not self._cfg.enabled:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_sync, subject, body, media_path)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_socket(self, host: str, port: int, timeout: float) -> Optional[socket.socket]:
        """
        Return a connected socket routed through SOCKS5 proxy if configured.
        Returns None to let smtplib use its default socket creation.
        """
        pcfg = self._proxy_cfg
        if pcfg is None or not pcfg.enabled or not pcfg.use_for_email:
            return None
        if pcfg.type not in {"socks5", "http"}:
            return None
        if not _SOCKS_OK:
            logger.warning(
                "Email proxy configured but PySocks is not installed.  "
                "Install it with:  pip install PySocks"
            )
            return None
        proxy_type = _socks.SOCKS5 if pcfg.type == "socks5" else _socks.HTTP
        return _socks.create_connection(
            (host, port),
            timeout=timeout,
            proxy_type=proxy_type,
            proxy_addr=pcfg.host,
            proxy_port=pcfg.port,
            proxy_username=pcfg.username or None,
            proxy_password=pcfg.password or None,
        )

    def _send_sync(
        self,
        subject: str,
        body: str,
        media_path: Optional[str] = None,
    ) -> None:
        """Blocking SMTP send to all configured recipients; runs in a thread executor."""
        cfg = self._cfg
        recipients = cfg.target_emails
        if not recipients:
            return

        msg = MIMEMultipart("mixed" if media_path else "alternative")
        msg["Subject"] = subject
        msg["From"] = f"{cfg.from_name} <{cfg.smtp_user}>"
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(body or "", "plain", "utf-8"))

        # Attach media file if provided
        if media_path and os.path.isfile(media_path):
            mime_type, _ = mimetypes.guess_type(media_path)
            maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
            with open(media_path, "rb") as fh:
                part = MIMEBase(maintype, subtype)
                part.set_payload(fh.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=os.path.basename(media_path),
            )
            msg.attach(part)

        context = ssl.create_default_context()
        raw = msg.as_string()
        sock = self._make_socket(cfg.smtp_host, cfg.smtp_port, timeout=30)

        try:
            mode = cfg.ssl_mode
            if mode == "starttls":
                smtp_cls = smtplib.SMTP
                with smtp_cls(cfg.smtp_host, cfg.smtp_port, timeout=30, source_address=None) as smtp:
                    if sock is not None:
                        smtp.sock = sock
                        smtp.file = smtp.sock.makefile("rb")
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                    smtp.sendmail(cfg.smtp_user, recipients, raw)
            elif mode == "none":
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
                    if sock is not None:
                        smtp.sock = sock
                        smtp.file = smtp.sock.makefile("rb")
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                    smtp.sendmail(cfg.smtp_user, recipients, raw)
            else:
                # Default: implicit TLS (SMTP_SSL)
                with smtplib.SMTP_SSL(
                    cfg.smtp_host, cfg.smtp_port, context=context,
                    timeout=30, sock=sock,
                ) as smtp:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                    smtp.sendmail(cfg.smtp_user, recipients, raw)
            logger.info(
                "E-mail sent to %s via %s [ssl_mode=%s]: %s",
                ", ".join(recipients), cfg.smtp_host, cfg.ssl_mode, subject,
            )
        except smtplib.SMTPAuthenticationError:
            logger.error(
                "E-mail authentication failed for %s@%s.  "
                "Check smtp_user and smtp_password in config.toml.",
                cfg.smtp_user, cfg.smtp_host,
            )
        except smtplib.SMTPConnectError as exc:
            logger.error(
                "Could not connect to SMTP server %s:%d – %s",
                cfg.smtp_host, cfg.smtp_port, exc,
            )
        except Exception:
            logger.exception(
                "Failed to send e-mail to %s via %s:%d [ssl_mode=%s]",
                ", ".join(recipients), cfg.smtp_host, cfg.smtp_port, cfg.ssl_mode,
            )
