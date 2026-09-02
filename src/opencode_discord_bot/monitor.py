"""Read-only opencode session monitor — posts Discord embeds on desktop events.

The bot's slash commands drive opencode sessions the user starts from
Discord, but opencode sessions running on the DESKTOP (a long task kicked
off in the opencode TUI/GUI, or any other prompt against the same
`opencode serve` instance the bot talks to) are invisible from Discord —
until they block on a permission or question, when they silently wait
forever. This module closes that gap: a background poll loop that watches
the opencode server's GLOBAL endpoints and posts an embed per event to a
configured Discord channel, so the user can step away and still know when
they need to come back to approve/answer something, or that a session
finished.

Events surfaced (one embed each):
  - Question pending   — a new request id appears in GET /question for a
    session not bound to a Discord channel (orange embed).
  - Permission pending — a new request id appears in GET /permission for a
    session not bound to a Discord channel (red embed).
  - Session completed  — a session observed "busy" in GET /session/status
    leaves the status map (the server deletes idle sessions), for a
    session not bound to a Discord channel (green embed with a snippet of
    the final assistant text).

READ-ONLY BY CONSTRUCTION: the loop only ever calls get_session_status /
list_questions / list_permissions / get_session / list_messages. It never
calls reply_question / reject_question / reply_permission / abort_session
— approvals stay at the desktop; this is visibility, not remote control.

Sessions bound in EITHER SessionRouter file (the main bot's
`.opencode-discord-bot-sessions.json` or the bridge's
`.opencode-discord-bridge-sessions.json`) are EXCLUDED: those sessions
already surface their questions/permissions/responses in their own
Discord channels via the button UI, so notifying again would be duplicate
noise.

Polling, not SSE: the bot abandoned the SSE stream deliberately (the v2
wire format proved fragile to parse — see `events.py` and the
`stream_events` deprecation note). The same three documented GETs the
button UI uses (`questions.py:poll_pending_requests`) are polled here at
`config.monitor_poll_interval_seconds`.

Lifecycle: spawned as an in-process `asyncio.create_task` from
`OpencodeBot.on_connect` (gated on `config.monitor_enabled` +
`config.monitor_channel_id`), crash-isolated by a guard wrapper in
`commands.py` (a monitor crash logs and dies without taking the bot
down), and cancelled + drained in `OpencodeBot.close` — mirroring the
Comulytic bridge's lifecycle exactly.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from opencode_discord_bot.config import config
from opencode_discord_bot.opencode_client import OpencodeClient
from opencode_discord_bot.text_utils import (
    _final_assistant_text,
    permission_block,
    question_block,
)

_log = logging.getLogger("bot.monitor")

# Max chars of the final assistant text included in a completion embed.
# Embed descriptions cap at 4096 chars; 300 keeps the notification compact
# (it's a ping, not a transcript — the full text is on the desktop).
_SNIPPET_MAX = 300


def _snippet(text: str, limit: int = _SNIPPET_MAX) -> str:
    """Truncate a completion snippet to <=limit chars (ellipsis included)."""
    if len(text) <= limit:
        return text
    # Leave room for the trailing ellipsis so the result never exceeds
    # the cap (embed descriptions are hard-limited by Discord at 4096).
    cut = text[: limit - 1]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip() + "…"


def question_embed(session_title: str, sid: str, request: dict) -> discord.Embed:
    """Build the orange 'question pending' embed for one question request."""
    blocks = [question_block(q) for q in (request.get("questions") or [])]
    description = (
        "\n\n".join(blocks) if blocks else "(no questions in request)"
    )
    embed = discord.Embed(
        title=f"❓ Question needs an answer — {session_title}",
        description=description,
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"session {sid}")
    return embed


def permission_embed(
    session_title: str, sid: str, request: dict
) -> discord.Embed:
    """Build the red 'permission pending' embed for one permission request."""
    embed = discord.Embed(
        title=f"🔐 Permission needed — {session_title}",
        description=permission_block(request),
        color=discord.Color.red(),
    )
    embed.set_footer(text=f"session {sid}")
    return embed


def completion_embed(
    session_title: str, sid: str, snippet: str
) -> discord.Embed:
    """Build the green 'session completed' embed with a response snippet."""
    description = _snippet(snippet) if snippet else "_(no text output)_"
    embed = discord.Embed(
        title=f"✅ Session completed — {session_title}",
        description=description,
        color=discord.Color.brand_green(),
    )
    embed.set_footer(text=f"session {sid}")
    return embed


async def _fetch_title(client: OpencodeClient, sid: str) -> str:
    """Best-effort session title for embed headers (never raises)."""
    try:
        session = await client.get_session(sid)
        if isinstance(session, dict):
            title = session.get("title")
            if title:
                return str(title)
    except Exception:  # noqa: BLE001 — title is cosmetic; never block the loop
        _log.debug("get_session(%s) failed for monitor title", sid, exc_info=True)
    return "(unknown)"


async def _fetch_snippet(client: OpencodeClient, sid: str) -> str:
    """Best-effort final assistant text for a completed session."""
    try:
        messages = await client.list_messages(sid)
        if isinstance(messages, list):
            return _final_assistant_text(messages)
    except Exception:  # noqa: BLE001 — snippet is cosmetic; never block the loop
        _log.debug("list_messages(%s) failed for monitor snippet", sid, exc_info=True)
    return ""


class _MonitorState:
    """Mutable per-loop state: seen request ids, busy-session tracking, and
    the cached notification channel (re-fetched when a send fails — e.g. the
    channel was deleted and recreated)."""

    def __init__(self) -> None:
        self.seen_questions: set[str] = set()
        self.seen_permissions: set[str] = set()
        # opencode session ids observed busy/retry; completion = the id
        # leaves the status map (the server removes idle sessions).
        self.busy: set[str] = set()
        self.channel: object | None = None
        self.channel_fetched = False


async def _get_channel(bot, state: _MonitorState):
    """Resolve + cache the notification channel via the bot's gateway.

    `bot.get_channel` (in-memory cache) is tried first — free after the
    first fetch; `bot.fetch_channel` (REST round-trip) only runs on the
    first resolution. Returns None if both fail (the monitor logs + skips
    this cycle rather than crash-looping).
    """
    if state.channel is not None:
        return state.channel
    if not state.channel_fetched:
        state.channel_fetched = True
        cached = None
        get_channel = getattr(bot, "get_channel", None)
        if callable(get_channel):
            try:
                cached = get_channel(config.monitor_channel_id)
            except Exception:  # noqa: BLE001
                cached = None
        if cached is None:
            try:
                state.channel = await bot.fetch_channel(config.monitor_channel_id)
            except Exception as e:  # noqa: BLE001 — channel may be missing/mis-set
                _log.warning(
                    "monitor: fetch_channel(%s) failed: %r — skipping",
                    config.monitor_channel_id,
                    e,
                )
        else:
            state.channel = cached
    return state.channel


async def _post(bot, state: _MonitorState, embed: discord.Embed) -> bool:
    """Send one embed to the monitor channel, with the optional @mention.

    Returns True on success. On a send failure the cached channel is
    cleared so the next event re-resolves it (handles deleted/recreated
    channels). The mention prefix (`<@id>`) rides in the message CONTENT,
    not the embed — embeds don't ping.
    """
    channel = await _get_channel(bot, state)
    if channel is None:
        return False
    content = (
        f"<@{config.monitor_user_id}>" if config.monitor_user_id else None
    )
    try:
        await channel.send(content=content, embed=embed)
        return True
    except Exception as e:  # noqa: BLE001 — a bad channel must not kill the loop
        _log.warning("monitor: send failed: %r — re-resolving channel", e)
        state.channel = None
        state.channel_fetched = False
        return False


def _excluded_sids(bot) -> set[str]:
    """Session ids bound to Discord channels in EITHER SessionRouter file.

    These sessions already surface their questions/permissions/responses in
    their own Discord channels (button UI + response posts), so the monitor
    skips them to avoid duplicate notifications. Reads are via the already-
    loaded in-memory maps — no disk I/O, no writes (the main bot only READS
    the bridge's file; the bridge owns writes to it).
    """
    excluded: set[str] = set()
    for router in (bot.router, bot.bridge_router):
        if router is None:
            continue
        # `SessionRouter.current` is keyed by channel id; the internal
        # `_map` is channel-id -> session-id. Either way, the values are
        # the session ids we must exclude.
        internal = getattr(router, "_map", None)
        if isinstance(internal, dict):
            for value in internal.values():
                if isinstance(value, str):
                    excluded.add(value)
    return excluded


async def _poll_once(bot, client: OpencodeClient, state: _MonitorState) -> None:
    """One monitor cycle: fetch the three global endpoints, post embeds for
    new events, update busy-session tracking. Never raises — every fetch
    failure is logged and treated as empty so the loop keeps running."""
    status_map_result, questions_result, permissions_result = (
        await asyncio.gather(
            client.get_session_status(),
            client.list_questions(),
            client.list_permissions(),
            return_exceptions=True,
        )
    )
    if isinstance(status_map_result, Exception):
        _log.warning("monitor: get_session_status failed: %r", status_map_result)
        status_map: dict = {}
    else:
        status_map = status_map_result or {}
    if isinstance(questions_result, Exception):
        _log.warning("monitor: list_questions failed: %r", questions_result)
        questions: list = []
    else:
        questions = questions_result or []
    if isinstance(permissions_result, Exception):
        _log.warning("monitor: list_permissions failed: %r", permissions_result)
        permissions: list = []
    else:
        permissions = permissions_result or []

    excluded = _excluded_sids(bot)

    # --- question + permission events (new, non-excluded request ids) ---
    for req in questions:
        if not isinstance(req, dict):
            continue
        rid = req.get("id", "")
        sid = req.get("sessionID", "")
        if not rid or rid in state.seen_questions or sid in excluded:
            continue
        state.seen_questions.add(rid)
        title = await _fetch_title(client, sid)
        await _post(bot, state, question_embed(title, sid, req))

    for req in permissions:
        if not isinstance(req, dict):
            continue
        rid = req.get("id", "")
        sid = req.get("sessionID", "")
        if not rid or rid in state.seen_permissions or sid in excluded:
            continue
        state.seen_permissions.add(rid)
        title = await _fetch_title(client, sid)
        await _post(bot, state, permission_embed(title, sid, req))

    # --- completion events (tracked busy sessions that left the map) ---
    # A session counts as running when its status entry is "busy" or
    # "retry" (a retrying session is still working). The server removes
    # idle sessions from the map, so a tracked id disappearing = completed.
    running = {
        sid
        for sid, status in status_map.items()
        if isinstance(status, dict) and status.get("type") in ("busy", "retry")
    }
    for sid in running - excluded:
        if sid not in state.busy:
            _log.info("monitor: tracking busy session %s", sid)
        state.busy.add(sid)
    for sid in list(state.busy):
        if sid not in running:
            # Left the map (idle) — completed. Excluded sessions are
            # dropped from tracking without a notification.
            state.busy.discard(sid)
            if sid in excluded:
                continue
            title = await _fetch_title(client, sid)
            snippet = await _fetch_snippet(client, sid)
            await _post(bot, state, completion_embed(title, sid, snippet))


async def run_monitor(bot) -> None:
    """The monitor's main loop — poll, post, sleep, forever.

    `bot` is a duck-typed stand-in for `OpencodeBot` needing only:
    ``.client`` (an OpencodeClient), ``.router`` (the main SessionRouter),
    ``.bridge_router`` (the bridge SessionRouter or None), and awaitable
    ``.fetch_channel(id)``. Kept that narrow deliberately so tests can
    drive the loop with fakes. Reads config per cycle so live tweaks
    (interval, user id) apply without a restart. Cancelled by
    `OpencodeBot.close` on shutdown.
    """
    client = bot.client
    state = _MonitorState()
    _log.info(
        "session monitor started (channel=%s, interval=%ss)",
        config.monitor_channel_id,
        config.monitor_poll_interval_seconds,
    )
    try:
        while True:
            try:
                await _poll_once(bot, client, state)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad cycle must not kill the loop
                _log.exception("monitor: poll cycle raised")
            await asyncio.sleep(config.monitor_poll_interval_seconds)
    finally:
        _log.info("session monitor stopped")