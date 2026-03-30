"""
Burst limiter: prevents message floods from producing a wall of individual
relay messages.

Behaviour
---------
* Normal (non-burst) messages are relayed **immediately** without delay.
* A burst is detected when a channel sends >= ``threshold`` messages within
  a sliding window of ``window_seconds`` seconds.
* Once a burst is active, further messages are **buffered**.
* After ``window_seconds`` of inactivity on that channel the buffer is
  flushed as a single combined message.
* If another wave of messages arrives before the flush fires, the timer is
  reset (so the flush always waits for the channel to go quiet).

Thread-safety: this class is purely asyncio-based and not thread-safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Callback signature:  async def flush(channel_id: str, text: str) -> None
FlushCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class _ChannelState:
    """Mutable per-channel burst state."""
    # Messages waiting to be flushed as a combined burst
    buffered: List[str] = field(default_factory=list)
    # Timestamps of all messages inside the current sliding window
    recent_times: List[float] = field(default_factory=list)
    # True while the channel is considered to be in burst mode
    in_burst: bool = False
    # Active deferred-flush coroutine task (only one at a time)
    flush_task: Optional[asyncio.Task] = None


class BurstLimiter:
    """
    Detects message bursts per channel and combines them.

    Usage::

        limiter = BurstLimiter(threshold=20, window_seconds=300)
        await limiter.process(channel_id, text, my_flush_callback)

    The flush callback is ``async (channel_id: str, text: str) -> None``.
    It is called either immediately (non-burst) or after the burst
    window expires (combined burst).
    """

    def __init__(
        self,
        threshold: int = 20,
        window_seconds: float = 300.0,
        separator: str = "\n---\n",
    ) -> None:
        self.threshold = threshold
        self.window = window_seconds
        self.separator = separator
        self._states: Dict[str, _ChannelState] = defaultdict(_ChannelState)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process(
        self,
        channel_id: str,
        message: str,
        flush_callback: FlushCallback,
    ) -> None:
        """
        Accept an incoming message for *channel_id*.

        Either calls *flush_callback* right away (normal mode) or buffers
        the message and schedules a deferred flush (burst mode).
        """
        state = self._states[channel_id]
        now = time.monotonic()

        # --- Maintain sliding window ---
        state.recent_times.append(now)
        cutoff = now - self.window
        state.recent_times = [t for t in state.recent_times if t >= cutoff]

        burst_now = len(state.recent_times) >= self.threshold

        if not burst_now and not state.in_burst:
            # Happy path: relay the message immediately
            await flush_callback(channel_id, message)
            return

        # --- Entering or continuing burst mode ---
        if not state.in_burst:
            logger.info(
                "Burst detected on channel %s: %d messages in %.0f s – buffering.",
                channel_id,
                len(state.recent_times),
                self.window,
            )
            state.in_burst = True

        state.buffered.append(message)

        # (Re)schedule the deferred flush; every new message postpones it
        self._reschedule_flush(channel_id, state, flush_callback)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reschedule_flush(
        self,
        channel_id: str,
        state: _ChannelState,
        flush_callback: FlushCallback,
    ) -> None:
        """Cancel any pending flush task and start a fresh one."""
        if state.flush_task is not None and not state.flush_task.done():
            state.flush_task.cancel()

        state.flush_task = asyncio.ensure_future(
            self._deferred_flush(channel_id, state, flush_callback)
        )

    async def _deferred_flush(
        self,
        channel_id: str,
        state: _ChannelState,
        flush_callback: FlushCallback,
    ) -> None:
        """Sleep for the burst window, then emit all buffered messages as one."""
        try:
            await asyncio.sleep(self.window)
        except asyncio.CancelledError:
            # Another message arrived; the rescheduled task will handle flush.
            return

        if not state.buffered:
            return

        combined = self.separator.join(state.buffered)
        count = len(state.buffered)

        # Reset channel state before calling the (possibly slow) callback
        state.buffered = []
        state.in_burst = False
        state.recent_times = []

        logger.info(
            "Flushing burst for channel %s: %d messages combined.", channel_id, count
        )
        try:
            await flush_callback(channel_id, combined)
        except Exception:
            logger.exception(
                "Error in burst flush callback for channel %s", channel_id
            )
