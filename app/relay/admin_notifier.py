"""
Administrator health notifications.

These notifications are separate from the optional message-by-message
``[email_relay]`` forwarding mode.  They reuse the SMTP transport settings from
``[email_relay]`` but send only operational alerts to the administrator
recipients configured in ``[admin_notifications]``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .config import AdminNotificationsConfig, EmailRelayConfig, ProxyConfig
from .email_relay import EmailRelay

logger = logging.getLogger(__name__)


class AdminNotifier:
    """Sends throttled operational alerts to configured administrators."""

    def __init__(
        self,
        cfg: AdminNotificationsConfig,
        smtp_cfg: EmailRelayConfig,
        proxy_cfg: Optional[ProxyConfig] = None,
    ) -> None:
        self._cfg = cfg
        self._state_file = Path(cfg.state_file)
        self._smtp_cfg = smtp_cfg
        self._relay: Optional[EmailRelay] = None

        if cfg.enabled and cfg.administrator_emails:
            relay_cfg = EmailRelayConfig(
                enabled=True,
                smtp_host=smtp_cfg.smtp_host,
                smtp_port=smtp_cfg.smtp_port,
                smtp_user=smtp_cfg.smtp_user,
                smtp_password=smtp_cfg.smtp_password,
                ssl_mode=smtp_cfg.ssl_mode,
                target_emails=list(cfg.administrator_emails),
                from_name=smtp_cfg.from_name,
                use_tls=smtp_cfg.use_tls,
            )
            self._relay = EmailRelay(relay_cfg, proxy_cfg=proxy_cfg)

    def _missing_smtp_fields(self) -> list[str]:
        missing: list[str] = []
        if not self._smtp_cfg.smtp_host:
            missing.append("email_relay.smtp_host")
        if not self._smtp_cfg.smtp_port:
            missing.append("email_relay.smtp_port")
        if not self._smtp_cfg.smtp_user:
            missing.append("email_relay.smtp_user")
        if not self._smtp_cfg.smtp_password:
            missing.append("email_relay.smtp_password")
        return missing

    def _load_state(self) -> dict[str, float]:
        state: dict[str, float] = {}
        if self._state_file.exists():
            try:
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state = {
                        str(k): float(v)
                        for k, v in raw.items()
                        if isinstance(v, (int, float))
                    }
            except Exception:
                logger.warning(
                    "Could not read admin notification state file: %s",
                    self._state_file,
                    exc_info=True,
                )
        return state

    def _save_state(self, state: dict[str, float]) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            logger.warning(
                "Could not write admin notification state file: %s",
                self._state_file,
                exc_info=True,
            )

    def _cooldown_active(self, key: str) -> bool:
        active = False
        cooldown_seconds = self._cfg.cooldown_minutes * 60
        if cooldown_seconds > 0:
            last_sent = self._load_state().get(key, 0.0)
            active = (time.time() - last_sent) < cooldown_seconds
        return active

    async def notify(
        self,
        key: str,
        subject: str,
        body: str,
        *,
        bypass_cooldown: bool = False,
    ) -> bool:
        """Send one administrator alert unless disabled or throttled."""
        sent = False
        if self._cfg.enabled:
            if not self._cfg.administrator_emails:
                logger.warning(
                    "Admin notifications are enabled but no administrator emails are configured."
                )
            elif self._relay is None:
                logger.warning("Admin notifications are enabled but SMTP relay is not initialized.")
            else:
                missing = self._missing_smtp_fields()
                if missing:
                    logger.error(
                        "Admin notification not sent; missing SMTP settings: %s",
                        ", ".join(missing),
                    )
                elif not bypass_cooldown and self._cooldown_active(key):
                    logger.info(
                        "Admin notification %r suppressed by %d minute cooldown.",
                        key, self._cfg.cooldown_minutes,
                    )
                else:
                    sent = await self._relay.send(f"[Aardvark] {subject}", body)
                    if sent:
                        state = self._load_state()
                        state[key] = time.time()
                        self._save_state(state)
                    else:
                        logger.error("Admin notification %r was not sent.", key)
        return sent

    async def send_test(self, body: str) -> bool:
        """Send a test notification regardless of cooldown."""
        return await self.notify(
            "admin-notification-test",
            "Administrator notification test",
            body,
            bypass_cooldown=True,
        )
