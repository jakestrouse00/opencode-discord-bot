"""Read-only opencode session monitor — posts Discord embeds on desktop events.

The bot's slash commands drive opencode sessions the user starts from
Discord, but opencode sessions running on the DESKTOP (a long task kicked
off in the opencode TUI/GUI, or any other prompt against the same
`opencode serve` instance the bot talks to) are invisible from Discord —
until they block on a permission or question, when they silently wait
forever. This module closes that gap: a background poll loop that watches
the opencode server and posts an embed per event to a configured Discord
channel, so the user can step away and still know when they need to come
back to approve/answer something, or that a session finished.

Events surfaced (one embed each):
  - Question pending   — a new request id appears in GET /question for a
    session not bound to a Discord channel (orange embed).
  - Permission pending — a new request id appears in GET /permission for a
    session not bound to a Discord channel (red embed).
  - Session completed  — a session observed "busy" in GET /session/status
    leaves the status map (the server deletes idle sessions), for a
    session not bound to a Discord channel (green embed with a snippet of
    the final assistant text).

PER-DIRECTORY POLLING: the question/permission/status maps are
instance-scoped on multi-instance opencode servers — a bare GET only sees
the serve process's cwd instance. When `config.monitor_all_directories`
(default True), each cycle discovers the server's known project
directories via GET /project and polls the three endpoints once per
directory (plus the unparameterized cwd poll as a belt-and-braces
fallback). Status maps are UNIONed (session ids are globally unique per
server, so union is correct); request-id dedup is shared across
directories, so the same pending request seen via the cwd poll AND its
directory poll posts exactly one embed.

READ-ONLY BY CONSTRUCTION: the loop only ever calls get_session_status /
list_questions / list_permissions / get_session / list_messages /
list_projects. It never calls reply_question / reject_question /
reply_permission / abort_session — approvals stay at the desktop; this is
visibility, not remote control.

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


def _directory_label(directory: str | None) -> str | None:
    r"""Short project label for embed footers — the basename of the worktree.

    Splits on BOTH `/` and `\`: the bot may run on Linux (Fly) while the
    server's worktrees are Windows paths, so `os.path.basename` would
    return the full path. Returns None for empty/None (no label).
    """
    if not directory:
        return None
    label = directory.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return label or None


def question_embed(
    session_title: str,
    sid: str,
    request: dict,
    directory_label: str | None = None,
) -> discord.Embed:
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
    footer = f"session {sid}"
    if directory_label:
        footer += f" · {directory_label}"
    embed.set_footer(text=footer)
    return embed


def permission_embed(
    session_title: str,
    sid: str,
    request: dict,
    directory_label: str | None = None,
) -> discord.Embed:
    """Build the red 'permission pending' embed for one permission request."""
    embed = discord.Embed(
        title=f"🔐 Permission needed — {session_title}",
        description=permission_block(request),
        color=discord.Color.red(),
    )
    footer = f"session {sid}"
    if directory_label:
        footer += f" · {directory_label}"
    embed.set_footer(text=footer)
    return embed


def completion_embed(
    session_title: str,
    sid: str,
    snippet: str,
    directory_label: str | None = None,
) -> discord.Embed:
    """Build the green 'session completed' embed with a response snippet."""
    description = _snippet(snippet) if snippet else "_(no text output)_"
    embed = discord.Embed(
        title=f"✅ Session completed — {session_title}",
        description=description,
        color=discord.Color.brand_green(),
    )
    footer = f"session {sid}"
    if directory_label:
        footer += f" · {directory_label}"
    embed.set_footer(text=footer)
    return embed


async def _fetch_title(
    client: OpencodeClient, sid: str, directory: str | None = None
) -> str:
    """Best-effort session title for embed headers (never raises).

    `directory` routes the GET to the project instance the session was
    seen in — session lookups are instance-scoped, so a bare call would
    miss (or 404) for a sid living in another directory.
    """
    try:
        session = await client.get_session(sid, directory=directory)
        if isinstance(session, dict):
            title = session.get("title")
            if title:
                return str(title)
    except Exception:  # noqa: BLE001 — title is cosmetic; never block the loop
        _log.debug("get_session(%s) failed for monitor title", sid, exc_info=True)
    return "(unknown)"


async def _fetch_snippet(
    client: OpencodeClient, sid: str, directory: str | None = None
) -> str:
    """Best-effort final assistant text for a completed session.

    `directory` routes the GET to the project instance the session was
    seen in — message lookups are instance-scoped like sessions.
    """
    try:
        messages = await client.list_messages(sid, directory=directory)
        if isinstance(messages, list):
            return _final_assistant_text(messages)
    except Exception:  # noqa: BLE001 — snippet is cosmetic; never block the loop
        _log.debug("list_messages(%s) failed for monitor snippet", sid, exc_info=True)
    return ""


async def _discover_directories(
    client: OpencodeClient, state: _MonitorState
) -> list[str | None]:
    """Directories to poll this cycle: `[None]` (the serve cwd instance)
    plus each known project worktree from `GET /project`.

    `None` first keeps the legacy unparameterized poll as a belt-and-braces
    fallback (it also covers servers without `/project` — a failure logs
    ONCE, not per cycle, and returns `[None]` so the monitor degrades to
    exact legacy behavior). Entries that are empty or `"/"` (the root
    pseudo-instance) are filtered — they can't host user sessions.
    """
    directories: list[str | None] = [None]
    try:
        projects = await client.list_projects()
    except Exception as e:  # noqa: BLE001 — discovery must not kill the loop
        if not state.projects_warned:
            _log.warning(
                "monitor: list_projects failed (%r) — falling back to "
                "cwd-only polling (this message logs once)",
                e,
            )
            state.projects_warned = True
        return directories
    for project in projects or []:
        if not isinstance(project, dict):
            continue
        worktree = project.get("worktree")
        if not isinstance(worktree, str) or worktree in ("", "/"):
            continue
        if worktree not in directories:
            directories.append(worktree)
    return directories


class _MonitorState:
    """Mutable per-loop state: seen request ids, busy-session tracking, the
    cached notification channel (re-fetched when a send fails — e.g. the
    channel was deleted and recreated), the per-sid directory map (which
    project instance a session was first seen in — fetches route there),
    and the one-shot `list_projects` failure flag."""

    def __init__(self) -> None:
        self.seen_questions: set[str] = set()
        self.seen_permissions: set[str] = set()
        # opencode session ids observed busy/retry; completion = the id
        # leaves the status map (the server removes idle sessions).
        self.busy: set[str] = set()
        # sid -> directory the sid was FIRST seen in (the poll that first
        # reported it wins; sids are globally unique per server, so there
        # is exactly one true directory — the map never needs updating).
        self.sid_directory: dict[str, str] = {}
        self.projects_warned = False
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
    """One monitor cycle: discover directories, fetch the three endpoints
    per directory, post embeds for new events, update busy-session
    tracking. Never raises — every fetch failure is logged and treated as
    empty so the loop keeps running.

    Status maps are UNIONed across directories (session ids are globally
    unique per server — a session lives in exactly one instance, so union
    is correct and never double-tracks). Question/permission request ids
    dedup against the SHARED seen-sets, so the same pending request seen
    via the cwd poll AND its directory poll posts exactly one embed.
    """
    if config.monitor_all_directories:
        directories = await _discover_directories(client, state)
    else:
        directories: list[str | None] = [None]

    # --- status maps: one GET per directory, unioned ---
    status_results = await asyncio.gather(
        *(
            client.get_session_status(directory=d)
            for d in directories
        ),
        return_exceptions=True,
    )
    status_map: dict = {}
    for d, result in zip(directories, status_results):
        if isinstance(result, BaseException):
            _log.warning(
                "monitor: get_session_status(directory=%r) failed: %r", d, result
            )
            continue
        for sid, status in (result or {}).items():
            # First poll to report a sid wins the directory mapping (the
            # cwd poll and the true directory's poll both see cwd sids —
            # identical state, either entry is correct).
            if sid not in state.sid_directory and d is not None:
                state.sid_directory[sid] = d
            status_map.setdefault(sid, status)

    # --- question + permission requests: one GET per directory ---
    question_results = await asyncio.gather(
        *(client.list_questions(directory=d) for d in directories),
        return_exceptions=True,
    )
    permission_results = await asyncio.gather(
        *(client.list_permissions(directory=d) for d in directories),
        return_exceptions=True,
    )
    questions: list = []
    for d, result in zip(directories, question_results):
        if isinstance(result, BaseException):
            _log.warning(
                "monitor: list_questions(directory=%r) failed: %r", d, result
            )
            continue
        for req in result or []:
            questions.append((d, req))
    permissions: list = []
    for d, result in zip(directories, permission_results):
        if isinstance(result, BaseException):
            _log.warning(
                "monitor: list_permissions(directory=%r) failed: %r", d, result
            )
            continue
        for req in result or []:
            permissions.append((d, req))

    excluded = _excluded_sids(bot)

    # --- question + permission events (new, non-excluded request ids) ---
    for d, req in questions:
        if not isinstance(req, dict):
            continue
        rid = req.get("id", "")
        sid = req.get("sessionID", "")
        if not rid or rid in state.seen_questions or sid in excluded:
            continue
        state.seen_questions.add(rid)
        directory = d if d is not None else state.sid_directory.get(sid)
        title = await _fetch_title(client, sid, directory)
        await _post(
            bot,
            state,
            question_embed(title, sid, req, _directory_label(directory)),
        )

    for d, req in permissions:
        if not isinstance(req, dict):
            continue
        rid = req.get("id", "")
        sid = req.get("sessionID", "")
        if not rid or rid in state.seen_permissions or sid in excluded:
            continue
        state.seen_permissions.add(rid)
        directory = d if d is not None else state.sid_directory.get(sid)
        title = await _fetch_title(client, sid, directory)
        await _post(
            bot,
            state,
            permission_embed(title, sid, req, _directory_label(directory)),
        )

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
            directory = state.sid_directory.get(sid)
            title = await _fetch_title(client, sid, directory)
            snippet = await _fetch_snippet(client, sid, directory)
            await _post(
                bot,
                state,
                completion_embed(title, sid, snippet, _directory_label(directory)),
            )


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