"""
State tracker: persists the last-relayed Telegram message ID for each
channel so that a service restart does not re-relay already-forwarded
messages.

The state is stored as a simple JSON file (default: relay_state.json).
Writes are atomic (write-then-rename) to prevent corruption on crash.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


class StateTracker:
    """
    Keeps track of the highest message ID that has been successfully
    relayed for each Telegram channel (keyed by channel ID string).

    Usage::

        tracker = StateTracker("relay_state.json")
        last_id = tracker.get_last_id("1202159807")   # 0 if never seen
        tracker.update("1202159807", 12345)            # persisted immediately
    """

    def __init__(self, state_file: str = "relay_state.json") -> None:
        self._path = Path(state_file)
        # channel_id (str) → highest relayed message ID (int)
        self._message_ids: Dict[str, int] = {}
        # channel_id (str) → Telegram profile photo fingerprint (str)
        self._photo_fingerprints: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_last_id(self, channel_id: str) -> int:
        """Return the highest relayed message ID for *channel_id*, or 0."""
        return self._message_ids.get(channel_id, 0)

    def update(self, channel_id: str, message_id: int) -> None:
        """
        Record that *message_id* has been relayed for *channel_id*.
        Only stores the value when it is higher than the current maximum
        (monotonic update).  Persists to disk immediately.
        """
        if message_id > self._message_ids.get(channel_id, 0):
            self._message_ids[channel_id] = message_id
            self._save()

    def get_photo_fingerprint(self, channel_id: str) -> str | None:
        """Return stored Telegram photo fingerprint for *channel_id*, or None."""
        return self._photo_fingerprints.get(channel_id)

    def update_photo_fingerprint(self, channel_id: str, fingerprint: str) -> None:
        """Store the latest Telegram photo fingerprint for *channel_id*."""
        if self._photo_fingerprints.get(channel_id) != fingerprint:
            self._photo_fingerprints[channel_id] = fingerprint
            self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load state from disk; silently start empty on any error."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            # Backward compatibility:
            # old schema: {channel_id: message_id}
            # new schema: {"message_ids": {...}, "photo_fingerprints": {...}}
            if isinstance(raw, dict) and "message_ids" in raw:
                self._message_ids = {
                    str(k): int(v) for k, v in raw.get("message_ids", {}).items()
                }
                self._photo_fingerprints = {
                    str(k): str(v)
                    for k, v in raw.get("photo_fingerprints", {}).items()
                }
            else:
                self._message_ids = {str(k): int(v) for k, v in raw.items()}
                self._photo_fingerprints = {}
            logger.debug(
                "State loaded from %s (%d message watermark(s), %d photo fingerprint(s))",
                self._path,
                len(self._message_ids),
                len(self._photo_fingerprints),
            )
        except Exception:
            logger.warning(
                "Could not load relay state from %s – starting fresh", self._path
            )
            self._message_ids = {}
            self._photo_fingerprints = {}

    def _save(self) -> None:
        """Atomically overwrite the state file."""
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(
                    {
                        "message_ids": self._message_ids,
                        "photo_fingerprints": self._photo_fingerprints,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception:
            logger.exception("Failed to save relay state to %s", self._path)
            tmp.unlink(missing_ok=True)
