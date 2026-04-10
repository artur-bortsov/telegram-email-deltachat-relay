"""
Telegram client: connects with user credentials (MTProto) and monitors
the configured channels for new messages.

Authentication is interactive on first run (Telethon prompts for the SMS
code); after that the session file is reused automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Message

from .config import ChannelsConfig, ProxyConfig, RelayConfig, TelegramConfig
from .state_tracker import StateTracker

# ---------------------------------------------------------------------------
# Optional PySocks import for proxy support
# ---------------------------------------------------------------------------
try:
    import socks as _socks  # type: ignore[import]  # from PySocks
    _SOCKS_OK = True
except ImportError:
    _socks = None  # type: ignore[assignment]
    _SOCKS_OK = False

logger = logging.getLogger(__name__)

# Callback: (channel_name, channel_id_str, text, media_path_or_None) → None
MessageCallback = Callable[[str, str, str, Optional[str]], Awaitable[None]]


class TelegramMonitor:
    """
    Authenticates with Telegram using user credentials, resolves the
    configured channels, relays historical messages on startup, and then
    forwards every new channel post to the registered callback.

    The session file (``<session_name>.session``) is created in the working
    directory and reused on subsequent runs.
    """

    def __init__(
        self,
        tg_cfg: TelegramConfig,
        ch_cfg: ChannelsConfig,
        relay_cfg: RelayConfig,
        state_tracker: Optional[StateTracker] = None,
        proxy_cfg: Optional[ProxyConfig] = None,
    ) -> None:
        self._tg_cfg = tg_cfg
        self._ch_cfg = ch_cfg
        self._relay_cfg = relay_cfg
        self._state_tracker = state_tracker
        self._proxy_cfg = proxy_cfg
        self._client: Optional[TelegramClient] = None
        # Raw entity objects keyed by their numeric ID (entity.id, no -100 prefix)
        self._entities: Dict[int, object] = {}
        # Human-readable name keyed by the same numeric ID
        self._channel_names: Dict[int, str] = {}
        # Bidirectional mapping: config identifier ↔ entity ID
        # (needed to diff old vs new watch lists on config reload)
        self._identifier_to_eid: Dict[str, int] = {}
        self._eid_to_identifier: Dict[int, str] = {}
        self._message_callback: Optional[MessageCallback] = None
        # Album grouping: buffer messages that share a grouped_id (Telegram media
        # albums) until a short timer fires, then dispatch them as a single relay
        # event instead of N individual messages.
        self._album_buffer: Dict[int, List[Message]] = {}             # grouped_id → buffered msgs
        self._album_flush_tasks: Dict[int, asyncio.TimerHandle] = {}  # pending flush timers
        self._album_window: float = relay_cfg.album_window_seconds
        self._album_mode: str = relay_cfg.album_mode
        # Serialization lock: guarantees that no two album dispatches run
        # concurrently.  Without this, two albums whose flush timers fire at the
        # same time would interleave their individual file messages in the relay
        # output (e.g. A-caption, B-caption, A-photo2, B-photo2 instead of the
        # correct A-caption, A-photo2, B-caption, B-photo2).
        # asyncio.Lock() is safe to create outside a coroutine on Python 3.10+.
        self._dispatch_lock = asyncio.Lock()
        # Per-channel activity tracking for the staleness watchdog.
        # Maps entity ID → monotonic timestamp of last received message.
        self._last_activity: Dict[int, float] = {}
        # Flag set to True while a watchdog-triggered reconnect is in progress.
        # run_forever() uses this to loop back to run_until_disconnected() with
        # the new client instead of exiting and triggering a full teardown.
        self._reconnect_in_progress: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _build_telethon_proxy(self) -> dict:
        """
        Build the ``proxy=`` / ``connection=`` kwargs for TelegramClient.

        Returns an empty dict when proxy is disabled or unavailable.
        SOCKS5/HTTP require PySocks; MTProto uses Telethon's built-in transport.
        """
        cfg = self._proxy_cfg
        if cfg is None or not cfg.enabled:
            return {}

        if cfg.type == "mtproto":
            try:
                from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
                # MTProto secret is passed as the proxy secret (password field).
                return {
                    "connection": ConnectionTcpMTProxyRandomizedIntermediate,
                    "proxy": (cfg.host, cfg.port, cfg.password),
                }
            except ImportError:
                logger.warning("MTProto proxy requested but Telethon MTProto connection class not found.")
                return {}

        if not _SOCKS_OK:
            logger.warning(
                "Telegram proxy configured but PySocks is not installed.  "
                "Install it with:  pip install PySocks"
            )
            return {}

        proxy_type = _socks.SOCKS5 if cfg.type == "socks5" else _socks.HTTP
        proxy_tuple = (
            proxy_type,
            cfg.host,
            cfg.port,
            cfg.rdns,
            cfg.username or None,
            cfg.password or None,
        )
        logger.info(
            "Telegram proxy: %s %s:%d", cfg.type.upper(), cfg.host, cfg.port
        )
        return {"proxy": proxy_tuple}

    @staticmethod
    def _tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
        """Return True when a TCP connection to host:port can be established."""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            code = sock.connect_ex((host, port))
            sock.close()
            return code == 0
        except Exception:
            return False

    def set_message_callback(self, callback: MessageCallback) -> None:
        """Register the async callback invoked for each message to relay."""
        self._message_callback = callback

    async def start(self) -> None:
        """Connect to Telegram, resolve channels, and begin listening."""
        # Check proxy reachability before attempting connection.
        if self._proxy_cfg and self._proxy_cfg.enabled and self._proxy_cfg.host:
            if self._tcp_reachable(self._proxy_cfg.host, self._proxy_cfg.port):
                logger.info(
                    "Telegram proxy %s:%d is reachable.",
                    self._proxy_cfg.host, self._proxy_cfg.port,
                )
            else:
                logger.warning(
                    "Telegram proxy %s:%d is NOT reachable.  "
                    "Connection will likely fail.  "
                    "Check proxy settings and network connectivity.",
                    self._proxy_cfg.host, self._proxy_cfg.port,
                )
        proxy_param = self._build_telethon_proxy()
        self._client = TelegramClient(
            self._tg_cfg.session_name,
            self._tg_cfg.api_id,
            self._tg_cfg.api_hash,
            **proxy_param,
        )
        # ``phone`` triggers SMS code login on first run
        await self._client.start(phone=self._tg_cfg.phone)
        logger.info("Telegram client connected (session: %s)", self._tg_cfg.session_name)

        await self._resolve_channels()
        self._register_event_handler()
        # Initialise activity timestamps so the watchdog has a baseline.
        self.reset_activity()
        await self._relay_history()

    async def run_forever(self) -> None:
        """
        Block until the Telegram connection is permanently stopped.

        If a watchdog reconnect is in progress when run_until_disconnected()
        returns, loop back and wait for the new client to be ready instead of
        exiting (which would trigger a full service teardown).
        """
        while True:
            await self._client.run_until_disconnected()
            if not self._reconnect_in_progress:
                # Normal stop() was called — exit cleanly.
                break
            # Watchdog reconnect in progress: new client is being set up.
            # Spin-wait until reconnect() clears the flag and the new
            # client is ready, then loop back to run_until_disconnected().
            while self._reconnect_in_progress:
                await asyncio.sleep(0.05)

    def get_min_idle_seconds(self) -> float:
        """
        Return the shortest idle time (in seconds) across all watched channels.

        If no activity has been recorded yet (e.g. right after startup), returns
        0.0 so the watchdog does not trigger prematurely.
        """
        if not self._last_activity:
            return 0.0
        now = time.monotonic()
        return min(now - t for t in self._last_activity.values())

    def is_connected(self) -> bool:
        """Return True if the Telethon client currently has an active MTProto connection."""
        return bool(self._client and self._client.is_connected())

    def reset_activity(self) -> None:
        """Mark all watched channels as active right now (called after reconnect)."""
        now = time.monotonic()
        for eid in self._channel_names:
            self._last_activity[eid] = now

    async def stop(self) -> None:
        """Gracefully disconnect the Telegram client."""
        # Ensure a pending reconnect flag does not block run_forever().
        self._reconnect_in_progress = False
        if self._client and self._client.is_connected():
            await self._client.disconnect()
            logger.info("Telegram client disconnected")

    async def reconnect(self) -> None:
        """
        Disconnect and reconnect the Telegram client, re-resolve channels,
        re-register event handlers, and replay recent history.

        Called by the staleness watchdog when no messages have been received
        from any watched channel for an extended period.  This forces Telethon
        to re-sync the update state (pts) with Telegram's servers.
        """
        logger.warning("Watchdog: initiating Telegram reconnect ...")
        # Set flag BEFORE disconnect so run_forever() sees it when
        # run_until_disconnected() returns and loops instead of exiting.
        self._reconnect_in_progress = True
        if self._client and self._client.is_connected():
            await self._client.disconnect()
            logger.info("Watchdog: old client disconnected")
        # Re-create the client so Telethon rebuilds all internal update state.
        proxy_param = self._build_telethon_proxy()
        self._client = TelegramClient(
            self._tg_cfg.session_name,
            self._tg_cfg.api_id,
            self._tg_cfg.api_hash,
            **proxy_param,
        )
        await self._client.start(phone=self._tg_cfg.phone)
        logger.info("Watchdog: Telegram client reconnected")
        # Clear resolved state so channels are re-resolved cleanly.
        self._entities.clear()
        self._channel_names.clear()
        self._identifier_to_eid.clear()
        self._eid_to_identifier.clear()
        await self._resolve_channels()
        self._register_event_handler()
        self.reset_activity()
        await self._relay_history()
        # Clear the flag AFTER the new client is fully ready so run_forever()
        # can loop back to run_until_disconnected() with the new client.
        self._reconnect_in_progress = False
        logger.info("Watchdog: reconnect complete, history replayed")

    def get_channel_names(self) -> Dict[int, str]:
        """Return a copy of the resolved channel-ID → name mapping."""
        return dict(self._channel_names)

    async def update_channels(self, new_watch_list: List[str]) -> Tuple[
        List[Tuple[int, str]], List[Tuple[int, str]]
    ]:
        """
        Update the set of monitored channels to match *new_watch_list*.

        Returns ``(added, removed)`` where each element is a list of
        ``(entity_id, channel_name)`` pairs.
        """
        old_identifiers = set(self._identifier_to_eid.keys())
        new_identifiers = set(new_watch_list)

        # --- Remove channels no longer in the list ---
        removed: List[Tuple[int, str]] = []
        for identifier in old_identifiers - new_identifiers:
            eid = self._identifier_to_eid.pop(identifier, None)
            if eid is not None:
                name = self._channel_names.pop(eid, str(eid))
                self._entities.pop(eid, None)
                self._eid_to_identifier.pop(eid, None)
                removed.append((eid, name))
                logger.info("Removed channel from monitoring: %r", name)

        # --- Add newly listed channels ---
        added: List[Tuple[int, str]] = []
        for identifier in new_identifiers - old_identifiers:
            try:
                entity = await self._client.get_entity(identifier)
                eid = entity.id
                name = getattr(entity, "title", str(identifier))
                self._entities[eid] = entity
                self._channel_names[eid] = name
                self._identifier_to_eid[identifier] = eid
                self._eid_to_identifier[eid] = identifier
                added.append((eid, name))
                logger.info("Added channel to monitoring: %r (id=%d)", name, eid)
            except Exception:
                logger.exception("Failed to add channel: %s", identifier)

        # Re-register the event handler with the updated entity list
        self._reregister_event_handler()
        self._ch_cfg = ChannelsConfig(watch=list(new_watch_list))
        return added, removed

    async def get_channel_photo(self, channel_id: int) -> Optional[str]:
        """
        Download the Telegram channel's profile photo to a temporary file
        and return the file path, or *None* if no photo is available.
        The caller is responsible for deleting the file after use.
        """
        entity = self._entities.get(channel_id)
        if entity is None:
            return None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="tg_relay_photo_")
            # Pass an explicit output path; Telethon appends the real extension.
            # Passing a bare directory can fail for channels with older photo formats.
            output_path = os.path.join(tmp_dir, "profile_photo")
            path = await self._client.download_profile_photo(entity, file=output_path)
            if path:
                logger.debug("Downloaded profile photo for channel %d → %s", channel_id, path)
            return str(path) if path else None
        except Exception:
            logger.exception("Failed to download profile photo for channel %d", channel_id)
            return None

    def get_channel_photo_fingerprint(self, channel_id: int) -> Optional[str]:
        """
        Return a stable fingerprint string for the Telegram channel's current
        profile photo, or *None* if the channel has no photo.

        This uses Telegram's own photo identifier metadata rather than the
        downloaded bytes, which avoids false positives caused by Delta Chat
        re-encoding images internally.
        """
        entity = self._entities.get(channel_id)
        if entity is None:
            return None
        photo = getattr(entity, "photo", None)
        if photo is None:
            return None
        # Telethon entity photos typically expose ``photo_id``. Fall back to
        # ``id`` and include dc_id/class name for a more robust fingerprint.
        photo_id = getattr(photo, "photo_id", None) or getattr(photo, "id", None)
        dc_id = getattr(photo, "dc_id", None)
        if photo_id is None:
            return None
        return f"{photo.__class__.__name__}:{photo_id}:{dc_id}"

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    async def _resolve_channels(self) -> None:
        """
        Resolve each entry in ``channels.watch`` to a Telethon entity.
        Entries can be @username strings, t.me/… URLs, or numeric IDs.
        """
        for identifier in self._ch_cfg.watch:
            try:
                entity = await self._client.get_entity(identifier)
                eid = entity.id
                name = getattr(entity, "title", str(identifier))
                self._entities[eid] = entity
                self._channel_names[eid] = name
                self._identifier_to_eid[identifier] = eid
                self._eid_to_identifier[eid] = identifier
                logger.info(
                    "Resolved channel: %s → id=%d, name=%r", identifier, eid, name
                )
            except Exception:
                logger.exception("Failed to resolve Telegram channel: %s", identifier)

        if not self._channel_names:
            logger.warning(
                "No channels resolved – check the [channels] watch list in config.toml"
            )

    # ------------------------------------------------------------------
    # Event handler registration
    # ------------------------------------------------------------------

    def _register_event_handler(self) -> None:
        """Attach a Telethon event handler for new messages in watched channels."""
        self._reregister_event_handler()

    def _reregister_event_handler(self) -> None:
        """
        Remove the existing NewMessage handler (if any) and register a
        fresh one covering the current entity list.  Safe to call at any
        time after the client is connected.
        """
        self._client.remove_event_handler(self._on_new_message, events.NewMessage)
        if not self._entities:
            logger.info("No channels to monitor.")
            return
        self._client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=list(self._entities.values())),
        )
        logger.info(
            "Listening for new messages in %d channel(s): %s",
            len(self._channel_names),
            list(self._channel_names.values()),
        )

    # ------------------------------------------------------------------
    # Historical messages
    # ------------------------------------------------------------------

    async def _relay_history(self) -> None:
        """Relay recent messages from each channel according to history_mode."""
        mode = self._relay_cfg.history_mode
        for eid, name in self._channel_names.items():
            try:
                messages = await self._fetch_history(eid, mode)

                # Drop messages already relayed in a previous run
                if self._state_tracker:
                    last_id = self._state_tracker.get_last_id(str(eid))
                    before = len(messages)
                    messages = [m for m in messages if m.id > last_id]
                    skipped = before - len(messages)
                    if skipped:
                        logger.info(
                            "Skipping %d already-relayed message(s) from '%s'",
                            skipped, name,
                        )

                if messages:
                    logger.info(
                        "Relaying %d new historical message(s) from '%s'",
                        len(messages), name,
                    )
                await self._relay_message_list(messages, name, str(eid))
            except Exception:
                logger.exception("Error fetching history for channel '%s'", name)

    async def _relay_message_list(
        self, messages: List[Message], channel_name: str, channel_id: str
    ) -> None:
        """
        Relay a flat list of messages, grouping album parts by their grouped_id.

        Telegram sends media albums (multiple photos/videos in one post) as
        separate Message objects sharing the same ``grouped_id``.  This method
        collects them into groups and passes each group to ``_dispatch_group``
        so they are forwarded as one relay event instead of N individual ones.
        Ungrouped messages are dispatched individually.
        """
        # Build an ordered list of groups, preserving first-occurrence order.
        seen_gids: Dict[int, int] = {}           # grouped_id → index in ordered_groups
        ordered_groups: List[List[Message]] = []

        for msg in messages:
            gid: Optional[int] = getattr(msg, "grouped_id", None)
            if gid is not None:
                if gid in seen_gids:
                    ordered_groups[seen_gids[gid]].append(msg)
                else:
                    seen_gids[gid] = len(ordered_groups)
                    ordered_groups.append([msg])
            else:
                ordered_groups.append([msg])

        for group in ordered_groups:
            await self._dispatch_group(group, channel_name, channel_id)

    async def _dispatch_group(
        self, msgs: List[Message], channel_name: str, channel_id: str
    ) -> None:
        """
        Process one message (or one album group) and invoke the relay callback.

        For a single message the behaviour is identical to the old per-message
        dispatch.  For a multi-message album group the behaviour depends on
        ``album_mode`` (configured via ``relay.album_mode`` in config.toml):

        * ``"first_only"`` (default) – one DC message: combined caption + first
          media file.  A ``[+N more …]`` note is appended when extra files exist.
          Extra downloaded temp files are removed immediately to save disk space.
        * ``"all_files"`` – one DC message per file, with the combined caption on
          the first message and no text on subsequent ones.
        """
        parts: List[Tuple[str, Optional[str]]] = []
        for msg in msgs:
            text, media_path = await self._process_media(msg)
            parts.append((text, media_path))

        # Combine all non-empty captions (usually only the first album item has text)
        combined_text = "\n".join(t for t, _ in parts if t).strip()

        if self._album_mode == "all_files":
            # Find the first part that has actual media so we can attach the
            # combined caption directly to it instead of sending a separate
            # text-only message.  Sending a text-only message is problematic
            # because it would go through the burst-limiter path in _on_message,
            # which can delay or reorder the caption relative to the videos, and
            # in some edge cases produce an empty DC message.
            first_with_media = next(
                (j for j, (_, mp) in enumerate(parts) if mp), None
            )
            for i, (msg_i, (_, media_path)) in enumerate(zip(msgs, parts)):
                if first_with_media is not None:
                    msg_text = combined_text if i == first_with_media else ""
                else:
                    # All media was oversized / failed; send the combined
                    # placeholder text once from the first part.
                    msg_text = combined_text if i == 0 else ""
                if (msg_text or media_path) and self._message_callback:
                    logger.info(
                        "Album part %d/%d → channel %r: text=%s, media=%s",
                        i + 1, len(msgs), channel_name,
                        repr(msg_text[:60]) if msg_text else "(none)",
                        "yes" if media_path else "no",
                    )
                    await self._message_callback(
                        channel_name, channel_id, msg_text, media_path
                    )
                    if self._state_tracker:
                        self._state_tracker.update(channel_id, msg_i.id)
        else:
            # first_only: one DC message – first available media file + combined caption
            first_media: Optional[str] = next((mp for _, mp in parts if mp), None)
            extra_count = sum(1 for _, mp in parts if mp) - (1 if first_media else 0)

            # Discard temp files for extra media that will not be forwarded
            for _, mp in parts:
                if mp and mp != first_media:
                    self._cleanup_media(mp)

            if extra_count > 0:
                note = f"[+{extra_count} more media file(s) in original post]"
                combined_text = (
                    f"{combined_text}\n{note}".strip() if combined_text else note
                )

            if (combined_text or first_media) and self._message_callback:
                await self._message_callback(
                    channel_name, channel_id, combined_text, first_media
                )
                if self._state_tracker:
                    self._state_tracker.update(channel_id, msgs[-1].id)

    async def _flush_album_group(
        self, grouped_id: int, channel_name: str, channel_id: str
    ) -> None:
        """
        Flush a buffered live album group after ``album_window_seconds`` of silence.
        Invoked via ``asyncio.call_later`` once no new parts arrive within the window.

        Acquires ``_dispatch_lock`` before dispatching so that two albums whose
        timers fire at the same time cannot interleave their messages.
        """
        msgs = self._album_buffer.pop(grouped_id, [])
        self._album_flush_tasks.pop(grouped_id, None)
        if not msgs:
            return
        msgs.sort(key=lambda m: m.id)
        logger.info(
            "Album group %d for channel %r: %d part(s), ids=%s",
            grouped_id, channel_name, len(msgs), [m.id for m in msgs],
        )
        if self._dispatch_lock.locked():
            logger.info(
                "Album group %d (channel %r): another album dispatch is in progress; "
                "queuing behind it to avoid interleaved messages.",
                grouped_id, channel_name,
            )
        async with self._dispatch_lock:
            await self._dispatch_group(msgs, channel_name, channel_id)
            logger.info(
                "Album group %d (channel %r): dispatch complete.", grouped_id, channel_name
            )

    async def _fetch_history(self, channel_id: int, mode: str) -> List[Message]:
        """Return historical messages in chronological order."""
        if mode == "last_n":
            limit = max(1, self._relay_cfg.history_last_n)
            msgs = await self._client.get_messages(channel_id, limit=limit)
            return list(reversed(msgs))          # oldest → newest

        if mode == "since_today":
            today_utc = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # Fetch a generous batch; filter by date locally
            msgs = await self._client.get_messages(channel_id, limit=200)
            filtered = [m for m in msgs if m.date and m.date >= today_utc]
            return list(reversed(filtered))

        logger.warning("Unknown history_mode %r – falling back to last 3", mode)
        msgs = await self._client.get_messages(channel_id, limit=3)
        return list(reversed(msgs))

    # ------------------------------------------------------------------
    # Live message handler
    # ------------------------------------------------------------------

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Telethon callback for every new message in a watched channel."""
        try:
            # event.chat.id is the raw entity ID (without -100 prefix)
            chat = await event.get_chat()
            eid = chat.id if chat else event.chat_id
            # Record activity for the watchdog.
            self._last_activity[eid] = time.monotonic()
            channel_name = self._channel_names.get(eid, str(eid))
            msg = event.message
            grouped_id: Optional[int] = getattr(msg, "grouped_id", None)

            if grouped_id is not None:
                # Buffer this album part and (re)schedule the flush timer so we
                # wait for the remaining parts before dispatching to the callback.
                self._album_buffer.setdefault(grouped_id, []).append(msg)
                logger.debug(
                    "Album group %d: part id=%d buffered for channel %r "
                    "(total=%d); flush timer (re)set in %.1f s.",
                    grouped_id, msg.id, channel_name,
                    len(self._album_buffer[grouped_id]),
                    self._album_window,
                )

                old_handle = self._album_flush_tasks.pop(grouped_id, None)
                if old_handle is not None:
                    old_handle.cancel()

                ch_id = str(eid)
                loop = asyncio.get_running_loop()
                self._album_flush_tasks[grouped_id] = loop.call_later(
                    self._album_window,
                    lambda: asyncio.ensure_future(
                        self._flush_album_group(grouped_id, channel_name, ch_id)
                    ),
                )
            else:
                text, media_path = await self._process_media(msg)
                if (text or media_path) and self._message_callback:
                    await self._message_callback(channel_name, str(eid), text, media_path)
                    # Update watermark so restarts don't re-relay this message
                    if self._state_tracker:
                        self._state_tracker.update(str(eid), msg.id)
        except Exception:
            logger.exception("Error handling new Telegram message")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(msg: Message) -> str:
        """
        Extract the text/caption from a Telegram message.

        Only stickers and polls get a text annotation since they cannot be
        forwarded as files.  All other media types are handled by
        ``_process_media``.
        """
        parts: List[str] = []
        if getattr(msg, "sticker", None):
            parts.append("[Sticker]")
        elif getattr(msg, "poll", None):
            parts.append("[Poll]")
        text = getattr(msg, "message", None) or getattr(msg, "caption", None) or ""
        if text:
            parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _has_downloadable_media(msg: Message) -> bool:
        """Return True when the message contains a file that can be downloaded."""
        return bool(
            getattr(msg, "photo", None)
            or getattr(msg, "video", None)
            or getattr(msg, "audio", None)
            or getattr(msg, "voice", None)
            or getattr(msg, "document", None)
        )

    @staticmethod
    def _media_type_label(msg: Message) -> str:
        """Return a human-readable media-type label for placeholder messages."""
        if getattr(msg, "photo", None):    return "Photo"
        if getattr(msg, "video", None):    return "Video"
        if getattr(msg, "audio", None):    return "Audio"
        if getattr(msg, "voice", None):    return "Voice message"
        if getattr(msg, "document", None): return "Document"
        return "Media"

    @staticmethod
    def _cleanup_media(media_path: str) -> None:
        """Remove a temporary media file that will not be forwarded."""
        try:
            parent = os.path.dirname(media_path)
            if os.path.basename(parent).startswith("tg_relay_"):
                shutil.rmtree(parent, ignore_errors=True)
            else:
                os.unlink(media_path)
        except Exception:
            pass

    async def _process_media(
        self, msg: Message
    ) -> Tuple[str, Optional[str]]:
        """
        Return ``(text, media_path)`` for a message.

        If the message has downloadable media within the configured size
        limit, ``media_path`` is a temp-file path (caller must delete it).
        If the media exceeds the limit, a placeholder note is appended to
        ``text`` and ``media_path`` is *None*.
        """
        text = self._extract_text(msg)

        if not self._has_downloadable_media(msg):
            return text, None

        max_mb = self._relay_cfg.max_media_size_mb
        file_size: Optional[int] = getattr(
            getattr(msg, "file", None), "size", None
        )

        # Size-limit check (skip when limit is 0 or size is unknown)
        if max_mb > 0 and file_size is not None and file_size > max_mb * 1024 * 1024:
            size_str = f"{file_size / (1024 * 1024):.1f} MB"
            label = self._media_type_label(msg)
            note = f"[{label} – {size_str}, not relayed (limit: {max_mb:.0f} MB)]"
            logger.info(
                "Skipping %s (%s) from channel – exceeds %.0f MB limit",
                label, size_str, max_mb,
            )
            return (f"{text}\n{note}".strip() if text else note), None

        # Download the file
        try:
            tmp_dir = tempfile.mkdtemp(prefix="tg_relay_")
            path = await self._client.download_media(msg, file=tmp_dir)
            return text, (str(path) if path else None)
        except Exception:
            logger.exception("Failed to download media from Telegram message")
            return text, None
