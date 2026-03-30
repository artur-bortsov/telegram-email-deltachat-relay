"""
Channel mapper: keeps track of Telegram-channel → Delta Chat invite link
mappings and writes them to a human-readable text file.

The file is overwritten atomically on every update so it is always
consistent even if the process is interrupted mid-write.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ChannelMapper:
    """
    Stores ``channel_name → invite_link`` pairs and persists them to a
    plain text file so users can find and share the Delta Chat join links
    without inspecting application logs.

    The file is re-written every time a new mapping is registered.
    """

    def __init__(self, invite_links_file: str) -> None:
        self._path = Path(invite_links_file)
        # channel_name → invite URL (or placeholder if unavailable)
        self._links: Dict[str, str] = {}

    def register(self, channel_name: str, invite_link: Optional[str]) -> None:
        """
        Store the invite link for *channel_name* and update the file on disk.
        If *invite_link* is *None* a placeholder is stored instead.
        """
        stored = invite_link if invite_link else "(invite link unavailable)"
        self._links[channel_name] = stored
        self._write()

    def get(self, channel_name: str) -> Optional[str]:
        """Return the stored invite link for *channel_name*, or *None*."""
        return self._links.get(channel_name)

    def all_links(self) -> Dict[str, str]:
        """Return a copy of all stored mappings."""
        return dict(self._links)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self) -> None:
        """
        Write (or overwrite) the invite links file.

        Uses a write-then-rename strategy to prevent a partially written
        file from being visible to readers.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            "# Telegram → Delta Chat relay – invite links\n",
            f"# Last updated: {timestamp}\n",
            "#\n",
            "# Open these links in your Delta Chat app to join the channel mirrors.\n",
            "# On desktop, use Menu → Scan QR Code and paste the link.\n\n",
        ]
        for name, link in sorted(self._links.items()):
            lines.append(f"## {name}\n")
            lines.append(f"{link}\n\n")

        tmp_path = self._path.with_suffix(".tmp")
        try:
            tmp_path.write_text("".join(lines), encoding="utf-8")
            # Atomic rename (works on POSIX; on Windows os.replace is atomic too)
            os.replace(tmp_path, self._path)
            logger.debug("Invite links written to %s", self._path)
        except Exception:
            logger.exception("Failed to write invite links file %s", self._path)
            # Clean up temp file if it exists
            tmp_path.unlink(missing_ok=True)
