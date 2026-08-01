"""OpencodeBot — the Discord command surface for the opencode control bot.

`OpencodeBot(discord.Client)` owns an `app_commands.CommandTree` of slash
commands (no prefix commands) PLUS a plain-text follow-up path via
`on_message`. Each `/oc` or `/oc_plan` invocation creates a fresh opencode
session AND a fresh Discord text channel under the configured category
(`discord_bot_session_category_id`), then posts the response there. Any
subsequent plain-text message in that session channel is forwarded to the
bound opencode session as a follow-up prompt. The bot ignores messages in
channels it did not create.

Slash commands defer the interaction (opencode prompts can take minutes),
stream progress back to the new channel via `poll_until_idle` (polling
`GET /session/status`), and reply with the final assistant message split
into <=2000-char chunks.

Reading the content of plain-text follow-up messages requires the privileged
`message_content` gateway intent, enabled in the Discord Developer Portal
(Bot -> Privileged Gateway Intents) and set on the bot via
`intents.message_content = True` in `__init__`.

Channel allowlist: if `discord_bot_allowed_channel_ids` is non-empty, only
those channels are honored for slash commands invoked OUTSIDE bot-created
session channels; bot-created session channels are always honored (they're
identified via `SessionRouter.current`). Other channels get an ephemeral
"not allowed" reply. Empty list = all channels allowed (for testing/personal use).

Run via `python -m bot` (see bot/__main__.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

import discord

from opencode_discord_bot.events import SessionStatus, poll_until_idle
from opencode_discord_bot.opencode_client import OpencodeClient, OpencodeError
from opencode_discord_bot.questions import poll_pending_requests
from opencode_discord_bot.session_router import SessionRouter
from opencode_discord_bot.slug import aclose_slug_client, generate_slug
from opencode_discord_bot.voice import (
    VoiceSession,
    extract_audio_to_wav,
    is_transcribable_attachment,
    transcribe_audio,
)
from opencode_discord_bot.opencode_serve import OpencodeServe, _REPO_ROOT
from opencode_discord_bot.config import config

_log = logging.getLogger("bot.commands")

# Discord message length cap (hard API limit). Long opencode responses must be
# split into chunks at most this long.
DISCORD_MSG_MAX = 2000

# Throttle for editing the "working…" progress message. discord.py's
# Message.edit is rate-limited (~5 edits/5s per channel); editing on every SSE
# event (dozens/sec during tool execution) would trip 429s. ~2s is safe.
PROGRESS_EDIT_MIN_INTERVAL = 2.0


def _split_message(text: str, limit: int = DISCORD_MSG_MAX) -> list[str]:
    """Split a long string into <=limit chunks, preferring code-block / newline boundaries.

    Order of preference: code-fence boundaries (```), double newlines, single
    newlines, then hard char splits. Each chunk is <= limit. Never returns an
    empty list (returns [""] for empty input).
    """
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        # try to split on a code-fence boundary near the limit
        cut = -1
        fence = remaining.rfind("\n```", 0, limit)
        if fence != -1:
            # include the closing fence line in this chunk
            cut = fence + 4
        else:
            para = remaining.rfind("\n\n", 0, limit)
            if para != -1:
                cut = para + 2
            else:
                nl = remaining.rfind("\n", 0, limit)
                if nl != -1:
                    cut = nl + 1
                else:
                    # last resort: hard split at limit
                    cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


def _extract_text(parts: list[dict]) -> str:
    """Concatenate all text parts from an opencode message's `parts` array."""
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            t = part.get("text")
            if t:
                out.append(t)
    return "\n".join(out) if out else ""


def _channel_allowed(channel_id: int) -> bool:
    allow = config.discord_bot_allowed_channel_ids
    if not allow:
        return True
    return channel_id in allow


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "This channel is not allowed for the opencode bot.", ephemeral=True
    )


# Discord text-channel name length cap (hard API limit).
_CHANNEL_NAME_MAX = 100


def _slugify_prompt(prompt: str, fallback: str) -> str:
    """Turn a prompt into a Discord channel name slug.

    Takes the first ~6 words, lowercases, collapses non-[a-z0-9-] runs to
    single hyphens, strips leading/trailing hyphens, and caps at
    `_CHANNEL_NAME_MAX` chars. Returns `fallback` if the prompt yields no
    usable slug (empty, all-symbols, etc.).
    """
    words = prompt.split()[:6]
    slug = "-".join(words).lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return fallback
    return slug[:_CHANNEL_NAME_MAX]


class OpencodeBot(discord.Bot):
    """Discord bot backing the opencode control bot slash commands.

    Owns the `opencode serve` subprocess lifecycle: `on_connect` starts the
    server (so the bot's `OpencodeClient` has something to talk to by the time
    it handles the first slash command), and `close` stops it. The password the
    server is spawned with is also seeded into this process's `os.environ` so
    `OpencodeClient._auth()` sends matching basic-auth on every request.

    Built on Pycord (`py-cord[voice]`) rather than mainline discord.py because
    Pycord provides the voice recording/sinks API (`VoiceClient.start_recording`
    / `discord.sinks.Sink`) the `/oc_voice` feature needs, which mainline
    discord.py removed in 2.4.0 and never restored. Pycord's command model is
    decorator-based (`@bot.slash_command` + `discord.Option`) rather than
    `app_commands.CommandTree`; this class migrated from the tree model to
    the decorator model when the dep switched from discord.py to Pycord.

    `auto_sync_commands=False` (set in `__init__`) disables Pycord's startup
    auto-sync so the bot doesn't push a *global* copy of every slash command
    on every login (which, combined with the guild-scoped commands pushed by
    `sync_commands.py`, produced duplicate entries in the Discord UI). Slash
    commands are synced explicitly via
    `python -m opencode_discord_bot.sync_commands --guild <id>` after the
    command surface changes.
    """

    def __init__(self) -> None:
        # `message_content` is the privileged intent required to read the
        # `content` of plain-text user messages in guild channels (without it
        # `message.content` is always "" except for self-messages, DMs, and
        # mentions). Must also be enabled in the Discord Developer Portal
        # (Bot -> Privileged Gateway Intents). `voice_states` is required to
        # populate `Member.voice.channel` so `/oc_voice` can discover which
        # voice channel the invoking user is in.
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        # `auto_sync_commands=False` disables Pycord's `on_connect` auto-sync
        # (BotBase.on_connect does `if self.auto_sync_commands: await
        # self.sync_commands()`), which was pushing a *global* copy of every
        # slash command on every startup. Combined with the guild-scoped
        # commands pushed by `sync_commands.py`, Discord's UI rendered both
        # sets → duplicate entries. Slash commands are now synced explicitly
        # via `python -m opencode_discord_bot.sync_commands --guild <id>`
        # (one-off, after command surface changes); the bot no longer touches
        # Discord's command registry on startup. See `on_connect` below.
        super().__init__(intents=intents, auto_sync_commands=False)
        self.client = OpencodeClient()
        self.router = SessionRouter()
        self._serve = OpencodeServe(cwd=str(_REPO_ROOT))
        # opencode session ids currently driving a prompt, mapped to the
        # running `_drive_session` asyncio Task so it can be cancelled by the
        # edit-to-revert handler (and other interrupt paths). Replaces the old
        # `_busy_sids: set[str]` which only supported "is it busy?" checks —
        # `on_message` still uses this for the membership test
        # (`if sid in self._active_drives`), and `_drive_session` registers/
        # unregisters the current task here. `on_message_edit` pops + cancels
        # the task to break out of `poll_until_idle` after aborting the session.
        self._active_drives: dict[str, asyncio.Task] = {}
        # Per-channel mapping of `discord_message_id -> opencode_message_id`
        # for user-authored prompts (plain-text follow-ups only — slash-command
        # prompts have no user Discord message to edit, so `/oc` and
        # `/oc_plan` are NOT mapped here). Keyed by channel id so the edit
        # handler can look up which opencode user message to revert to when a
        # user edits a follow-up Discord message. After a revert-and-resend,
        # the same Discord message id is re-pointed at the new opencode
        # message id so repeated edits keep working.
        self._prompt_msg_map: dict[int, dict[int, str]] = {}
        # Active voice sessions keyed by guild_id — prevents duplicate voice
        # sessions per guild and provides the handle for `/oc_voice_stop`.
        self._voice_sessions: dict[int, VoiceSession] = {}
        self._serve_started = False  # guards `on_connect` against reconnect re-spawn
        self._register_commands()

    def _register_commands(self) -> None:
        @self.slash_command(
            name="oc",
            description="Send a prompt to opencode and wait for the response.",
        )
        async def oc(
            ctx: discord.ApplicationContext,
            prompt: str = discord.Option(str, "The prompt to send to opencode."),
        ) -> None:
            await self._run_prompt(ctx, prompt, agent=None)

        @self.slash_command(
            name="oc_plan",
            description="Send a change to opencode's plan-author subagent (reasoned, plan-only).",
        )
        async def oc_plan(
            ctx: discord.ApplicationContext,
            change: str = discord.Option(str, "The change to draft a plan for."),
            plan_type: str | None = discord.Option(
                str,
                "Optional: pick 'Planned update' or 'Note to self'. Leave blank to let the agent classify.",
                choices=[
                    discord.OptionChoice("Planned update", "actionable"),
                    discord.OptionChoice("Note to self", "note"),
                ],
                default=None,
            ),
        ) -> None:
            prompt = change
            if plan_type is not None:
                directive = (
                    "[PLAN_TYPE_PRESELECTED: actionable]"
                    if plan_type == "actionable"
                    else "[PLAN_TYPE_PRESELECTED: note]"
                )
                prompt = f"{directive}\n\n{change}"
            await self._run_prompt(ctx, prompt, agent="plan-author", echo_prompt=change)

        @self.slash_command(
            name="oc_new",
            description="Reset this channel's opencode session (start fresh).",
        )
        async def oc_new(ctx: discord.ApplicationContext) -> None:
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            await self.router.reset(ctx.channel_id)
            await ctx.respond(
                f"Session binding reset for channel {ctx.channel_id}. "
                "Plain-text messages here will no longer be forwarded. "
                "Use `/oc` to create a new session channel."
            )

        @self.slash_command(
            name="oc_session",
            description="Show this channel's current opencode session + status.",
        )
        async def oc_session(ctx: discord.ApplicationContext) -> None:
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            await ctx.defer()
            sid = self.router.current(ctx.channel_id)
            if sid is None:
                await ctx.followup.send(
                    "No opencode session bound to this channel. Use `/oc` to create one."
                )
                return
            try:
                session = await self.client.get_session(sid)
            except OpencodeError as e:
                await ctx.followup.send(f"Failed to fetch session `{sid}`: {e}")
                return
            title = session.get("title", "(no title)")
            status = (
                (session.get("status") or {}).get("value", "unknown")
                if isinstance(session.get("status"), dict)
                else str(session.get("status", "unknown"))
            )
            await ctx.followup.send(
                f"Session `{sid}`\nTitle: {title}\nStatus: {status}"
            )

        @self.slash_command(
            name="oc_sessions", description="List recent opencode sessions."
        )
        async def oc_sessions(ctx: discord.ApplicationContext) -> None:
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            await ctx.defer()
            try:
                sessions = await self.client.list_sessions()
            except OpencodeError as e:
                await ctx.followup.send(f"Failed to list sessions: {e}")
                return
            recent = sessions[:10]
            lines = [
                f"`{s.get('id', '?')}` — {s.get('title', '(no title)')}" for s in recent
            ]
            text = "\n".join(lines) if lines else "(no sessions)"
            for chunk in _split_message(
                f"**Recent opencode sessions ({len(recent)} of {len(sessions)}):**\n{text}"
            ):
                await ctx.followup.send(chunk)

        @self.slash_command(
            name="oc_abort",
            description="Abort the running opencode session bound to this channel.",
        )
        async def oc_abort(ctx: discord.ApplicationContext) -> None:
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            await ctx.defer()
            sid = self.router.current(ctx.channel_id)
            if sid is None:
                await ctx.followup.send("No opencode session bound to this channel.")
                return
            try:
                ok = await self.client.abort_session(sid)
            except OpencodeError as e:
                await ctx.followup.send(f"Failed to abort session `{sid}`: {e}")
                return
            await ctx.followup.send(f"Abort requested for session `{sid}`: {ok}.")

        @self.slash_command(
            name="oc_voice",
            description="Join a voice channel, transcribe your spoken plan/note, route to plan-author.",
        )
        async def oc_voice(
            ctx: discord.ApplicationContext,
            mode: str = discord.Option(
                str,
                "Pick 'Planned update' (actionable plan) or 'Note to self' (note).",
                choices=[
                    discord.OptionChoice("Planned update", "change"),
                    discord.OptionChoice("Note to self", "note"),
                ],
                required=True,
            ),
            voice_channel: discord.VoiceChannel | None = discord.Option(
                discord.VoiceChannel,
                "Voice channel to join (defaults to your current channel).",
                default=None,
                required=False,
            ),
        ) -> None:
            await self._start_voice_session(ctx, mode, voice_channel)

        @self.slash_command(
            name="oc_voice_stop",
            description="Manually stop the active voice session and process the transcript.",
        )
        async def oc_voice_stop(ctx: discord.ApplicationContext) -> None:
            session = self._voice_sessions.get(ctx.guild_id)
            if session is None:
                await ctx.respond("No active voice session in this guild.")
                return
            await ctx.defer()
            transcript = await session.stop()
            await self._finalize_voice_session(ctx, session, transcript)

        @self.slash_command(
            name="oc_talk",
            description="Upload an audio/video recording of your thoughts; extract audio, transcribe, route to plan-author.",
        )
        async def oc_talk(
            ctx: discord.ApplicationContext,
            recording: discord.Attachment = discord.Option(
                discord.Attachment,
                "Audio or video recording of your plan/note (mp3, wav, m4a, ogg, mov, mp4, …).",
                required=True,
            ),
            plan_type: str | None = discord.Option(
                str,
                "Optional: pick 'Planned update' or 'Note to self'. Leave blank to let the agent classify.",
                choices=[
                    discord.OptionChoice("Planned update", "actionable"),
                    discord.OptionChoice("Note to self", "note"),
                ],
                default=None,
                required=False,
            ),
        ) -> None:
            await self._run_talk_session(ctx, recording, plan_type)

        @self.slash_command(
            name="oc_help", description="List the opencode bot commands."
        )
        async def oc_help(ctx: discord.ApplicationContext) -> None:
            text = (
                "**opencode bot commands**\n"
                "/oc `<prompt>` — create a new session channel and send the prompt.\n"
                "/oc_plan `<change>` — same, but routed to the plan-author subagent "
                "(drafts a reasoned change plan; writes only to .opencode/plans/).\n"
                "   • optional `plan_type`: 'Planned update' or 'Note to self' "
                "(otherwise the agent classifies from your wording).\n"
                "/oc_voice `<mode> [voice_channel]` — join a voice channel, listen to "
                "your spoken plan/note, transcribe and route it to the plan-author "
                "agent. `mode`: 'Planned update' or 'Note to self'. Say 'Stop "
                "Conversation', use `/oc_voice_stop`, or wait 10s of silence to "
                "finish. The bot speaks its response back in the voice channel (if "
                "TTS is enabled).\n"
                "/oc_voice_stop — manually stop the active voice session and process "
                "the transcript.\n"
                "/oc_talk `<recording> [plan_type]` — upload an audio or video recording "
                "of your thoughts; transcribe and route to the plan-author agent (same "
                "as /oc_plan but voice-in, no live channel join). For video files, the "
                "audio track is extracted via ffmpeg and only the audio is transcribed. "
                "`plan_type`: 'Planned update' or 'Note to self' (optional). Accepts "
                "mp3, wav, m4a, ogg, opus, webm, flac, mov, mp4, avi, mkv.\n"
                "**Voice messages:** send a Discord voice message (press-hold the mic in "
                "the mobile composer) in a session channel to transcribe it as a "
                "follow-up, or in the #new-plans trigger channel to start a new "
                "plan-author session. Same pipeline as /oc_talk, no slash command "
                "needed.\n"
                "/oc_new — unbind this channel's session (stop forwarding plain-text here).\n"
                "/oc_session — show this channel's current session + status.\n"
                "/oc_sessions — list recent opencode sessions.\n"
                "/oc_abort — abort the running session bound to this channel.\n"
                "/oc_help — this message.\n\n"
                "**Follow-ups:** in a channel created by `/oc` or `/oc_plan`, just type a "
                "plain-text message — it's forwarded to that channel's opencode session. "
                "One follow-up at a time (a busy session replies with a wait notice)."
            )
            await ctx.respond(text)

    async def on_connect(self) -> None:
        """Start `opencode serve` after the gateway connects.

        Replaces the old discord.py `setup_hook` (which Pycord does not call).
        `on_connect` fires after the gateway connection is established and before
        READY. Pycord's `BotBase.on_connect` only does
        `if self.auto_sync_commands: await self.sync_commands()` — since
        `auto_sync_commands=False` is set in `__init__`, there's nothing to
        delegate, so we intentionally do NOT call `super().on_connect()` (it
        would be a no-op anyway, but skipping it keeps the intent explicit).
        Slash commands are synced via the standalone `sync_commands.py` script,
        not on every startup. The serve subprocess is started here (not in
        __init__) so the spawn + health-check waits run on the bot's asyncio
        loop. If the server can't start (opencode not installed, port taken,
        timeout), the bot still logs in — subsequent `/oc` calls will fail
        with a clear OpencodeError.

        `on_connect` can fire multiple times on reconnect, so `_serve_started`
        guards against spawning a second subprocess.

        The password `OpencodeServe` spawns the server with is seeded into
        this process's `os.environ` so `OpencodeClient._auth()` sends matching
        basic-auth on every request (the client reads the env var at request
        time, not import time, so setting it here is sufficient).
        """
        # No `await super().on_connect()` here — `auto_sync_commands=False`
        # (set in __init__) makes it a no-op, and skipping it documents that
        # the bot does NOT push commands on startup. Sync via
        # `python -m opencode_discord_bot.sync_commands` instead.
        if self._serve_started:
            return
        self._serve_started = True
        import os

        if self._serve.enabled:
            # Seed the auth env var BEFORE starting the server so the client
            # (constructed in __init__ but used at request time) sends matching
            # basic-auth. An explicit env-var override already in os.environ
            # wins over the settings default.
            os.environ.setdefault("OPENCODE_SERVER_PASSWORD", self._serve._password)
            # start() is synchronous (subprocess + urllib polling); run it in a
            # thread so we don't block the bot's event loop during the health
            # check window (up to startup_timeout seconds).
            await asyncio.to_thread(self._serve.start)

    async def close(self) -> None:
        """Stop voice sessions + the opencode serve subprocess, then close the gateway.

        `discord.Client.close` is the canonical shutdown hook (called on
        KeyboardInterrupt in __main__.py and on logout). Stopping voice clients
        and the server first ensures clean teardown — no orphaned voice
        connections and a clean TCP close for in-flight HTTP requests.
        """
        # Disconnect any active voice clients so the bot doesn't appear "still
        # in voice" to Discord after the process exits.
        for session in list(self._voice_sessions.values()):
            try:
                vc = session.voice_client
                if vc.is_connected():
                    await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001 — teardown must not raise
                _log.warning("voice disconnect raised during close", exc_info=True)
        self._voice_sessions.clear()
        try:
            # `stop()` does blocking subprocess.run + proc.wait (up to ~8s on
            # Windows tree-kill). Offload it so the event loop keeps draining
            # pending voice disconnects / replies during shutdown.
            await asyncio.to_thread(self._serve.stop)
        except Exception:  # noqa: BLE001 — teardown must not raise
            _log.warning("opencode serve stop raised during close", exc_info=True)
        # Release the shared slug httpx.AsyncClient (best-effort).
        await aclose_slug_client()
        await super().close()

    def _channel_ok(self, channel_id: int | None) -> bool:
        """True if a slash command may run in this channel.

        A channel is OK if it's in the static allowlist OR it's a bot-managed
        session channel (has a bound opencode session via `SessionRouter`).
        The static allowlist gates commands invoked in pre-existing channels;
        bot-created session channels are always honored so `/oc_session`,
        `/oc_abort`, etc. work inside them even when the allowlist is set.
        """
        if channel_id is None:
            return False
        if self.router.current(channel_id) is not None:
            return True
        return _channel_allowed(channel_id)

    def _voice_attachment(self, message: discord.Message) -> discord.Attachment | None:
        """Return the first transcribable attachment on `message`, else None.

        Centralizes "is this a voice message?" detection so `on_message` can
        branch cleanly and the download path has a single attachment handle
        to `.read()`. Reuses `is_transcribable_attachment` from `voice.py`
        (already imported above) so the accepted-types list (`audio/*`,
        `video/*`, `application/ogg`, plus extension fallbacks) stays in one
        place. Discord voice messages arrive as `audio/ogg` /
        `application/ogg` attachments with usually-empty `content`.
        """
        for att in message.attachments:
            try:
                if is_transcribable_attachment(att):
                    return att
            except Exception:  # noqa: BLE001 — defensive: bad attachment metadata
                _log.warning(
                    "is_transcribable_attachment raised on %r; skipping",
                    getattr(att, "filename", "?"),
                )
        return None

    async def _rename_when_slug_ready(
        self,
        channel: discord.TextChannel,
        prompt: str,
        fallback: str,
    ) -> None:
        """Generate an LLM slug from `prompt` and rename `channel` to it.

        Shared create-then-rename helper used by the four entry points
        (`_run_prompt`, `_finalize_voice_session`, `_run_talk_session`, and
        indirectly `_start_voice_session` via the finalize step). Calls
        `generate_slug` (one short chat completion on the small cloud model),
        and if the result differs from the channel's current name and is
        non-empty, edits the channel name via `TextChannel.edit`. Best-effort:
        a failed rename (Discord HTTP error, rate limit, missing permission)
        logs a WARNING and leaves the initial name in place — channel creation
        is never blocked by this call (callers fire it via
        `asyncio.create_task`, not `await`).

        `fallback` is the regex slug the channel was created with; it's passed
        through to `generate_slug` as the fallback if the LLM call fails, and
        also guards against renaming to an empty/identical slug.
        """
        try:
            slug = await generate_slug(prompt, fallback=fallback)
        except (
            Exception
        ) as e:  # noqa: BLE001 — generate_slug is supposed to never raise, but defend in depth
            _log.warning(
                "generate_slug raised unexpectedly; keeping %r: %r", channel.name, e
            )
            return
        if not slug or slug == channel.name:
            return
        try:
            await channel.edit(name=slug, reason="LLM slug upgrade")
        except discord.HTTPException as e:
            _log.warning(
                "channel rename to %r failed (keeping %r): %r", slug, channel.name, e
            )

    async def _drive_session(
        self,
        sid: str,
        *,
        send_chunk: Callable[[str], Awaitable[Any]],
        progress_msg: discord.Message,
        voice_session: VoiceSession | None = None,
    ) -> str | None:
        """Shared poll/progress/reply body for `/oc` and plain-text follow-ups.

        Marks `sid` busy (so `on_message` rejects concurrent follow-ups for the
        same session), runs the throttled progress-edit loop against
        `progress_msg`, polls `GET /session/status` until idle or timeout,
        then fetches the final assistant message and posts it via
        `send_chunk` (chunked into <=2000-char pieces by `_split_message`).

        `send_chunk` is a one-arg awaitable that posts a single string to the
        target surface — `ctx.followup.send` for the slash path, `channel.send`
        for the follow-up path. Both return an editable `discord.Message`, but
        only `progress_msg` is edited here.

        `voice_session`, when set, routes pending question requests through
        the voice channel (TTS speaks the question, STT captures the answer)
        instead of Discord buttons. See `poll_pending_requests`.

        Returns the final assistant text that was posted to `send_chunk`, or
        None if the session produced no text or the message fetch failed. The
        voice-finalize path uses this to drive TTS without re-fetching the
        message list (the fetch inside this method already did that work).
        """
        self._active_drives[sid] = asyncio.current_task()  # type: ignore[assignment]
        # The request poller surfaces pending question/permission requests
        # for this session as Discord buttons/selects (or via voice when
        # `voice_session` is set). Runs concurrently with `poll_until_idle` and
        # is cancelled when the session goes idle (or times out). See
        # bot.questions.poll_pending_requests.
        stop_event = asyncio.Event()
        poller = asyncio.create_task(
            poll_pending_requests(
                self.client,
                sid,
                progress_msg.channel,
                interval=2.0,
                stop_event=stop_event,
                voice_session=voice_session,
            )
        )
        try:
            last_edit = 0.0
            last_status_text = ""

            async def on_status(status: SessionStatus) -> None:
                nonlocal last_edit, last_status_text
                text = _status_to_progress_text(status)
                if not text or text == last_status_text:
                    return
                last_status_text = text
                now = time.monotonic()
                if now - last_edit < PROGRESS_EDIT_MIN_INTERVAL:
                    return
                last_edit = now
                try:
                    await progress_msg.edit(
                        content=f"Working on session `{sid}`…\n{text}"
                    )
                except discord.HTTPException as e:
                    _log.warning("progress edit failed: %r", e)

            # Poll GET /session/status until the session goes idle (or
            # timeout). A timeout ensures a stuck session doesn't block
            # forever — we fall through to list_messages and reply with
            # whatever exists. CancelledError = /oc_abort or bot shutdown.
            try:
                await poll_until_idle(
                    self.client, sid, on_status, interval=2.0, timeout=1800.0
                )
            except asyncio.TimeoutError:
                _log.warning(
                    "session %s did not go idle within timeout; replying with partial output",
                    sid,
                )
            except asyncio.CancelledError:
                pass

            try:
                messages = await self.client.list_messages(sid)
            except OpencodeError as e:
                await send_chunk(f"Failed to fetch final messages: {e}")
                return None
            final_text = _final_assistant_text(messages)
            if not final_text:
                await send_chunk(f"Done (no text output). Session `{sid}` is now idle.")
                return None
            prefix = f"**opencode** (session `{sid}`):\n"
            for chunk in _split_message(prefix + final_text):
                await send_chunk(chunk)
            return final_text
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(poller, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                poller.cancel()
            self._active_drives.pop(sid, None)

    async def _run_prompt(
        self,
        ctx: discord.ApplicationContext,
        prompt: str,
        *,
        agent: str | None,
        echo_prompt: str | None = None,
    ) -> None:
        """Shared body for /oc and /oc_plan.

        Creates a fresh opencode session AND a fresh Discord text channel
        (under `discord_bot_session_category_id`), binds them, sends the
        prompt, replies to the interaction with a pointer to the new channel,
        and drives the session to completion with the response posted in the
        new channel.
        """
        if not self._channel_ok(ctx.channel_id):
            await _deny(ctx)
            return
        await ctx.defer()

        guild = ctx.guild
        if guild is None:
            await ctx.followup.send(
                "This command can only be used inside a server (guild)."
            )
            return

        # Create the opencode session first so we can use its id as a slug
        # fallback and in the channel topic.
        try:
            session = await self.client.create_session(title="discord-pending")
        except OpencodeError as e:
            await ctx.followup.send(f"Failed to create opencode session: {e}")
            return
        sid = session["id"]
        short_sid = sid[:8] if sid else "unknown"
        slug = _slugify_prompt(prompt, fallback=f"oc-{short_sid}")

        # Resolve the target category (best-effort; create with no parent if
        # unset or not found).
        category: discord.CategoryChannel | None = None
        cat_id = config.discord_bot_session_category_id
        if cat_id:
            resolved = guild.get_channel(cat_id)
            if isinstance(resolved, discord.CategoryChannel):
                category = resolved
            elif resolved is not None:
                _log.warning(
                    "discord_bot_session_category_id %d resolved to %s, not a "
                    "CategoryChannel; creating channel with no parent",
                    cat_id,
                    type(resolved).__name__,
                )
            else:
                _log.warning(
                    "discord_bot_session_category_id %d not found in guild; "
                    "creating channel with no parent",
                    cat_id,
                )

        try:
            new_channel = await guild.create_text_channel(
                name=slug,
                category=category,
                topic=f"opencode session {sid} (started by {ctx.user})",
                reason=f"/oc slash command by {ctx.user}",
            )
        except discord.HTTPException as e:
            await ctx.followup.send(f"Failed to create session channel `{slug}`: {e}")
            return

        # Best-effort LLM slug upgrade: the channel was created immediately
        # with the regex `slug` (so the user gets the pointer fast); this
        # fire-and-forget task renames it to a real LLM-generated slug once
        # the short chat completion lands (typically 1-3s). `slug_source` is
        # `echo_prompt or prompt` — NOT `prompt` directly — because `/oc_plan`
        # passes a directive-prefixed `prompt` (`[PLAN_TYPE_PRESELECTED: ...]
        # \n\n<change>`) but the clean user text in `echo_prompt`; a slug from
        # the raw `prompt` would be polluted with the directive prefix. `/oc`
        # passes no `echo_prompt`, so this falls through to the clean `prompt`.
        slug_source = echo_prompt or prompt
        asyncio.create_task(
            self._rename_when_slug_ready(new_channel, slug_source, fallback=slug)
        )

        # Bind the new channel to the session BEFORE sending any message, so
        # a user message arriving in the channel before the prompt completes
        # still resolves to the right session (and gets the busy reply).
        await self.router.bind(new_channel.id, sid)

        parts = [{"type": "text", "text": prompt}]
        try:
            await self.client.send_prompt_async(sid, parts, agent=agent)
        except OpencodeError as e:
            await ctx.followup.send(
                f"Created {new_channel.mention} but failed to send prompt to session `{sid}`: {e}"
            )
            await new_channel.send(f"Failed to send initial prompt: {e}")
            return

        # Echo the user's original request text in the new channel (opt-in;
        # only /oc_plan passes echo_prompt) so they can see/copy what they
        # wrote. Fenced code block gives Discord's native copy button.
        if echo_prompt is not None:
            await new_channel.send(f"**Your request:**\n```\n{echo_prompt}\n```")

        # Initial "working…" message in the new channel (edited with progress).
        progress_msg = await new_channel.send(f"Working on session `{sid}`…")

        # Point the slash-command user at the new channel; the full response
        # will be posted there (not echoed back to the interaction).
        await ctx.followup.send(f"Created {new_channel.mention} — continuing there.")

        await self._drive_session(
            sid, send_chunk=new_channel.send, progress_msg=progress_msg
        )

    async def on_message(self, message: discord.Message) -> None:
        """Forward plain-text messages in session channels as follow-up prompts,
        and detect Discord voice messages (press-hold mic in the mobile
        composer) for transcription + routing.

        Four branches (checked in order):

        (a) Session channel + voice attachment: transcribe and send the
            transcript as a follow-up prompt to the bound session
            (``_run_voice_followup``).
        (b) Session channel + no voice attachment: the existing plain-text
            follow-up path. Empty ``content`` is ignored (preserves the old
            silent-drop for non-voice empty messages). Non-empty content is
            forwarded via ``_run_followup``.
        (c) Non-session channel + voice attachment + the channel is the
            configured voice-message trigger channel
            (``config.voice_message_trigger_channel_id``): start a new
            plan-author session from the transcript (``_run_talk_from_message``).
            The trigger channel is #new-plans (id 1533242090862149842) by
            default; 0 disables the new-session path.
        (d) Else: ignored (non-session, non-voice — the same behavior as
            before this feature).

        Voice messages are regular ``discord.Message`` objects with an
        ``audio/ogg`` / ``application/ogg`` attachment and usually empty
        ``content``. The old guard ``if not message.content: return`` silently
        dropped them; the rewrite keeps bot-authored messages out, then
        routes on the (sid, voice_att) tuple. ``voice_message_enabled``
        (config, default True) gates both voice paths so the feature can be
        disabled without a code change.
        """
        if message.author.bot:
            return
        sid = self.router.current(message.channel.id)
        voice_att = (
            self._voice_attachment(message) if config.voice_message_enabled else None
        )
        # (a) Voice follow-up in an existing session channel.
        if sid is not None and voice_att is not None:
            if sid in self._active_drives:
                await message.channel.send(
                    "Session is busy, please wait for the current response to finish."
                )
                return
            await self._run_voice_followup(message, sid, voice_att)
            return
        # (b) Text follow-up in an existing session channel (existing path).
        if sid is not None and voice_att is None:
            if not message.content:
                return
            if sid in self._active_drives:
                await message.channel.send(
                    "Session is busy, please wait for the current response to finish."
                )
                return
            await self._run_followup(message, sid)
            return
        # (c) Voice message in the configured trigger channel -> new session.
        if (
            sid is None
            and voice_att is not None
            and config.voice_message_trigger_channel_id
            and message.channel.id == config.voice_message_trigger_channel_id
        ):
            await self._run_talk_from_message(message, voice_att)
            return
        # (d) Non-session, non-voice (or trigger channel not configured) — ignored.
        return

    async def _fetch_last_user_message_id(
        self, sid: str, *, seen_before: str | None = None, attempts: int = 3
    ) -> str | None:
        """Best-effort fetch of the latest opencode user message id in `sid`.

        `send_prompt_async` is fire-and-forget (204), so the opencode user
        message is created server-side during the forked prompt effect — there
        is a race where `list_messages` called immediately after may not yet
        see the new user message. This helper retries a few times with a short
        sleep, filtering for `role == "user"` messages with an `id` greater
        than `seen_before` (the previously-known last user message id, if
        any), and returns the new user message id or None.

        Used by `_run_followup` (and `on_message_edit`) to record/update the
        `_prompt_msg_map` mapping that powers edit-to-revert. A None return is
        non-fatal — the bot still functions; edit-to-revert just won't work
        for that one message.
        """
        for _ in range(attempts):
            try:
                messages = await self.client.list_messages(sid)
            except OpencodeError:
                return None
            last_user_id: str | None = None
            for entry in messages:
                if not isinstance(entry, dict):
                    continue
                info = entry.get("info") or {}
                if info.get("role") != "user":
                    continue
                mid = info.get("id")
                if not mid:
                    continue
                if seen_before is not None and mid <= seen_before:
                    continue
                last_user_id = mid
            if last_user_id is not None:
                return last_user_id
            await asyncio.sleep(0.5)
        return None

    async def _run_followup(self, message: discord.Message, sid: str) -> None:
        """Send a plain-text follow-up to an existing session and post the reply.

        Mirrors `_run_prompt`'s drive loop but skips session/channel creation
        (both already exist) and the slash-interaction pointer. The follow-up
        is routed to the default agent (no `agent=` override) — plan-mode
        sessions stay plan-mode on the opencode side.

        After sending, records `self._prompt_msg_map[channel.id][message.id]
        = opencode_user_message_id` so `on_message_edit` can revert to the
        right opencode message when the user edits this Discord follow-up.
        The mapping is best-effort — if the user message id can't be fetched
        (race, server hiccup), None is recorded and edit-to-revert silently
        no-ops for that message (the bot still functions).
        """
        parts = [{"type": "text", "text": message.content}]
        try:
            await self.client.send_prompt_async(sid, parts)
        except OpencodeError as e:
            await message.channel.send(
                f"Failed to send follow-up to session `{sid}`: {e}"
            )
            return

        # Best-effort: map this Discord follow-up message to the opencode
        # user message it just created, so a later edit can revert to it.
        prev_map = self._prompt_msg_map.get(message.channel.id, {})
        seen_before = prev_map.get(message.id)  # None for a first-time mapping
        new_user_id = await self._fetch_last_user_message_id(
            sid, seen_before=seen_before
        )
        if new_user_id is not None:
            self._prompt_msg_map.setdefault(message.channel.id, {})[
                message.id
            ] = new_user_id

        progress_msg = await message.channel.send(f"Working on session `{sid}`…")
        await self._drive_session(
            sid,
            send_chunk=message.channel.send,
            progress_msg=progress_msg,
            voice_session=None,
        )

    async def _run_voice_followup(
        self,
        message: discord.Message,
        sid: str,
        attachment: discord.Attachment,
    ) -> None:
        """Transcribe a voice-message attachment and send it as a follow-up prompt
        to an existing session (the voice-in analog of `_run_followup`).

        Same session, same drive loop, same edit-to-revert mapping, but the
        prompt text comes from Whisper instead of ``message.content``. Routed
        to the session's existing agent (no ``agent=`` override — follow-ups
        stay on whatever agent the session was started with, matching
        ``_run_followup``).

        Flow: post a "Transcribing voice message…" status → download the
        attachment → normalize to WAV via ``extract_audio_to_wav`` →
        ``transcribe_audio`` → empty guard (edit the status to a hint and
        return) → edit the status to show the transcript → send the transcript
        to the session via ``send_prompt_async`` → record the
        ``_prompt_msg_map`` entry so edit-to-revert works for voice follow-ups
        too → drive the session to completion.
        """
        status_msg = await message.channel.send("Transcribing voice message…")
        try:
            media_bytes = await attachment.read()
            wav_bytes = await extract_audio_to_wav(
                media_bytes,
                content_type=attachment.content_type,
                filename=attachment.filename,
            )
            transcript = await transcribe_audio(wav_bytes)
        except Exception as e:  # noqa: BLE001 — transcription failures are user-facing
            _log.warning("voice-message follow-up transcription failed: %r", e)
            await status_msg.edit(
                content=f"Failed to transcribe `{attachment.filename}`: {e}. "
                f"The session is bound to this channel — type your plan as "
                f"text and it will be forwarded to the same opencode session."
            )
            return

        transcript = (transcript or "").strip()
        if not transcript:
            await status_msg.edit(
                content="Transcription came back empty (no speech detected). "
                "The session is bound to this channel — type your plan as "
                "text to continue."
            )
            return

        # Send the transcript to the session's existing agent (no override).
        parts = [{"type": "text", "text": transcript}]
        try:
            await self.client.send_prompt_async(sid, parts)
        except OpencodeError as e:
            await message.channel.send(
                f"Failed to send voice follow-up to session `{sid}`: {e}"
            )
            return

        # Best-effort: map this Discord voice message to the opencode user
        # message it just created, so a later edit can revert to it. Mirrors
        # the text follow-up mapping in `_run_followup` (editing a voice
        # message is rare, but the mapping should be consistent).
        prev_map = self._prompt_msg_map.get(message.channel.id, {})
        seen_before = prev_map.get(message.id)
        new_user_id = await self._fetch_last_user_message_id(
            sid, seen_before=seen_before
        )
        if new_user_id is not None:
            self._prompt_msg_map.setdefault(message.channel.id, {})[
                message.id
            ] = new_user_id

        await status_msg.edit(
            content=f"**Transcribed prompt:**\n```\n{transcript}\n```"
        )
        progress_msg = await message.channel.send(f"Working on session `{sid}`…")
        await self._drive_session(
            sid,
            send_chunk=message.channel.send,
            progress_msg=progress_msg,
            voice_session=None,
        )

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Abort, revert, and resend when a user edits their own follow-up.

        Pycord dispatches `message_edit` (resolved to `on_message_edit` via
        `dispatch`) when a cached message is edited (`state.py:808`). This
        handler replicates the opencode GUI's "stop, revert, edit, resend"
        flow, but triggered by a Discord message edit instead of a GUI button:

        1. Ignore bot-authored messages and non-session channels.
        2. Look up the opencode user message id this Discord message was
           mapped to (via `_prompt_msg_map`); if no mapping, return (the
           edit didn't correspond to a follow-up we tracked, e.g. a message
           from before the bot started, or a slash-command echo).
        3. If the session is busy driving another prompt, cancel that drive
           task and abort the server-side work, then wait briefly for the
           abort to propagate (`revert` requires the session to not be busy).
        4. Revert the session to the mapped opencode message id.
        5. Send the edited text as a fresh prompt.
        6. Re-map the same Discord message id at the new opencode user
           message id (so repeated edits keep working).
        7. Post a fresh progress message and drive the new session.

        Concurrency guard (step 9 of the plan): if a new drive somehow started
        for this session between the cancel and the resend, skip to avoid
        overlapping edit-and-resend operations corrupting the session state.

        Only fires for plain-text follow-ups — `/oc` and `/oc_plan` prompts
        come from slash commands (no user-authored Discord message to edit),
        so they are never recorded in `_prompt_msg_map` and are ignored here.
        """
        # (a) Ignore bot-authored messages.
        if after.author.bot:
            return
        if before.content == after.content:
            return  # no-op edit (e.g. embed/thumbnail change only)
        # (b) Not a session channel?
        sid = self.router.current(after.channel.id)
        if sid is None:
            return
        # (c) No mapping for this Discord message?
        opencode_msg_id = self._prompt_msg_map.get(after.channel.id, {}).get(after.id)
        if opencode_msg_id is None:
            return
        # (d) Abort + cancel the running drive task if busy.
        try:
            if sid in self._active_drives:
                task = self._active_drives.pop(sid, None)
                if task is not None and not task.done():
                    task.cancel()
                try:
                    await self.client.abort_session(sid)
                except OpencodeError as e:
                    _log.warning("abort before edit-and-resend failed: %r", e)
                # `revert` requires the session to not be busy; give the
                # abort a moment to propagate to idle.
                await asyncio.sleep(1.0)
            # (e) Revert to the mapped opencode user message.
            await self.client.revert_session(sid, opencode_msg_id)
        except OpencodeError as e:
            # (step 8) A failed revert leaves the session in an inconsistent
            # state; surface the error and do NOT update the mapping so the
            # user knows their edit didn't take effect.
            try:
                await after.channel.send(
                    f"Edit-and-resend failed: {e}. The session may need to be "
                    f"reset with `/oc_new`."
                )
            except discord.HTTPException:
                _log.warning("failed to post edit-and-resend error", exc_info=True)
            return
        # (f) Send the edited text as a fresh prompt.
        parts = [{"type": "text", "text": after.content}]
        try:
            await self.client.send_prompt_async(sid, parts)
        except OpencodeError as e:
            try:
                await after.channel.send(
                    f"Edit-and-resend: revert succeeded but sending the new "
                    f"prompt failed: {e}."
                )
            except discord.HTTPException:
                _log.warning("failed to post edit-and-resend send error", exc_info=True)
            return
        # (g) Re-map the same Discord message id at the new opencode user
        # message id so repeated edits keep working.
        new_user_id = await self._fetch_last_user_message_id(
            sid, seen_before=opencode_msg_id
        )
        if new_user_id is not None:
            self._prompt_msg_map.setdefault(after.channel.id, {})[
                after.id
            ] = new_user_id
        # (step 9) Concurrency guard: if a new drive somehow started for this
        # session between the cancel and now, skip to avoid overlapping
        # edit-and-resend operations corrupting the session state.
        if sid in self._active_drives:
            try:
                await after.channel.send(
                    "Session is already processing an edit, please wait."
                )
            except discord.HTTPException:
                _log.warning("failed to post edit concurrency notice", exc_info=True)
            return
        # (h) Post a fresh progress message and drive the new session.
        try:
            progress_msg = await after.channel.send(
                f"Working on session `{sid}` (edited)…"
            )
        except discord.HTTPException as e:
            _log.warning("failed to post edit-and-resend progress: %r", e)
            return
        await self._drive_session(
            sid,
            send_chunk=after.channel.send,
            progress_msg=progress_msg,
            voice_session=None,
        )

    async def _start_voice_session(
        self,
        ctx: discord.ApplicationContext,
        mode: str,
        voice_channel: discord.VoiceChannel | None,
    ) -> None:
        """Entry point for `/oc_voice` — join a voice channel and start listening.

        Reuses the `/oc_plan` session+channel creation pattern (`create_session`
        → `_slugify_prompt` → resolve category → `create_text_channel` →
        `router.bind`), then connects to the target voice channel, constructs a
        `VoiceSession`, stores it in `_voice_sessions` (keyed by guild_id), and
        starts recording. The transcript isn't sent to opencode yet — that
        happens in `_finalize_voice_session` after a stop trigger fires.
        """
        if not self._channel_ok(ctx.channel_id):
            await _deny(ctx)
            return
        guild = ctx.guild
        if guild is None:
            await ctx.respond("This command can only be used inside a server (guild).")
            return
        if ctx.guild_id in self._voice_sessions:
            await ctx.respond(
                "A voice session is already active in this guild. "
                "Use `/oc_voice_stop` to finish it first."
            )
            return

        # Resolve the target voice channel: explicit param, else the invoking
        # user's current voice channel.
        target_vc = voice_channel
        if target_vc is None:
            user_voice = getattr(ctx.user, "voice", None)
            if user_voice is None or user_voice.channel is None:
                await ctx.respond(
                    "You're not in a voice channel. Join one, or pass `voice_channel`."
                )
                return
            target_vc = user_voice.channel

        await ctx.defer()

        # Create the opencode session + text channel (mirrors _run_prompt).
        try:
            session = await self.client.create_session(title="discord-voice")
        except OpencodeError as e:
            await ctx.followup.send(f"Failed to create opencode session: {e}")
            return
        sid = session["id"]
        short_sid = sid[:8] if sid else "unknown"
        slug = _slugify_prompt(target_vc.name, fallback=f"oc-voice-{short_sid}")

        category: discord.CategoryChannel | None = None
        cat_id = config.discord_bot_session_category_id
        if cat_id:
            resolved = guild.get_channel(cat_id)
            if isinstance(resolved, discord.CategoryChannel):
                category = resolved

        try:
            new_channel = await guild.create_text_channel(
                name=slug,
                category=category,
                topic=f"opencode voice session {sid} (started by {ctx.user})",
                reason=f"/oc_voice slash command by {ctx.user}",
            )
        except discord.HTTPException as e:
            await ctx.followup.send(f"Failed to create session channel `{slug}`: {e}")
            return
        await self.router.bind(new_channel.id, sid)

        # Connect to the voice channel. Pycord's VoiceChannel.connect() returns
        # a discord.voice.VoiceClient with the recording API.
        try:
            voice_client = await target_vc.connect()
        except discord.ClientException as e:
            await ctx.followup.send(f"Failed to join {target_vc.mention}: {e}")
            return
        except Exception as e:  # noqa: BLE001 — voice connect can raise various errors
            await ctx.followup.send(f"Failed to join {target_vc.mention}: {e}")
            return

        session_obj = VoiceSession(
            bot=self,
            voice_client=voice_client,
            text_channel=new_channel,
            session_id=sid,
            mode=mode,
            finalize_callback=self._finalize_voice_session,
        )
        self._voice_sessions[ctx.guild_id] = session_obj
        try:
            await session_obj.start()
        except Exception as e:  # noqa: BLE001 — recording start can fail
            await ctx.followup.send(f"Failed to start recording: {e}")
            # Clean up the half-built session.
            self._voice_sessions.pop(ctx.guild_id, None)
            try:
                if voice_client.is_connected():
                    await voice_client.disconnect(force=True)
            except Exception:
                pass
            return

        mode_label = "Planned update" if mode == "change" else "Note to self"
        await new_channel.send(
            f"Listening in {target_vc.mention} — speak your {mode_label}. "
            "Say 'Stop Conversation', use `/oc_voice_stop`, or wait "
            f"{config.voice_silence_timeout_seconds:.0f}s of silence to finish."
        )
        await ctx.followup.send(
            f"Created {new_channel.mention} — voice session active in "
            f"{target_vc.mention}. Continuing in the text channel."
        )

    async def _finalize_voice_session(
        self,
        ctx: discord.ApplicationContext | None,
        session: VoiceSession,
        transcript: str,
    ) -> None:
        """Bridge the voice transcript to the existing text session pipeline.

        Called from `/oc_voice_stop` (with `ctx`) or from `VoiceSession`'s
        auto-stop triggers (with `ctx=None` — the session's stored context is
        used). Strips the "stop conversation" phrase, builds the prompt with the
        `[PLAN_TYPE_PRESELECTED]` directive for note mode, sends it to the
        opencode session, drives the session to completion, optionally speaks
        the response via TTS, then disconnects and removes the voice session.
        """
        sid = session.session_id
        text_channel = session.text_channel
        # Strip the stop phrase (case-insensitive) from the transcript.
        cleaned = transcript
        for phrase in ("stop conversation", "Stop Conversation"):
            cleaned = cleaned.replace(phrase, "")
        cleaned = cleaned.strip()
        if not cleaned:
            await text_channel.send(
                "Transcript was empty (nothing detected). Voice session ended."
            )
            await self._teardown_voice_session(session)
            return

        # Best-effort LLM slug upgrade from the now-available transcript.
        # The text channel was created with the voice channel's name as the
        # initial slug (see `_start_voice_session`); now that the transcript
        # exists, generate a real slug from it and rename. Fire-and-forget so
        # it doesn't delay the opencode prompt send. `fallback=text_channel.
        # name` keeps the voice-channel-derived name if the LLM call fails.
        asyncio.create_task(
            self._rename_when_slug_ready(
                text_channel, cleaned, fallback=text_channel.name
            )
        )

        prompt = cleaned
        if session.mode == "note":
            prompt = f"[PLAN_TYPE_PRESELECTED: note]\n\n{cleaned}"

        parts = [{"type": "text", "text": prompt}]
        try:
            await self.client.send_prompt_async(sid, parts, agent="plan-author")
        except OpencodeError as e:
            await text_channel.send(
                f"Failed to send voice prompt to session `{sid}`: {e}"
            )
            await self._teardown_voice_session(session)
            return

        await text_channel.send(f"**Transcribed prompt:**\n```\n{cleaned}\n```")
        progress_msg = await text_channel.send(f"Working on session `{sid}`…")
        final_text = await self._drive_session(
            sid,
            send_chunk=text_channel.send,
            progress_msg=progress_msg,
            voice_session=session,
        )

        # TTS playback of the final response (best-effort). `_drive_session`
        # already fetched the message list and extracted the assistant text
        # for its own reply; reuse that here instead of re-fetching.
        if (
            config.voice_tts_enabled
            and final_text
            and session.voice_client.is_connected()
        ):
            try:
                await session.speak(final_text)
            except Exception as e:  # noqa: BLE001 — TTS is best-effort
                _log.warning("TTS playback failed: %r", e)

        await self._teardown_voice_session(session)

    async def _teardown_voice_session(self, session: VoiceSession) -> None:
        """Disconnect the voice client and remove the session from the registry."""
        gid = session.voice_client.guild.id if session.voice_client.guild else None
        if gid is not None:
            self._voice_sessions.pop(gid, None)
        try:
            if session.voice_client.is_connected():
                await session.voice_client.disconnect(force=True)
        except Exception:  # noqa: BLE001 — teardown must not raise
            _log.warning("voice disconnect during teardown failed", exc_info=True)

    async def _run_talk_session(
        self,
        ctx: discord.ApplicationContext,
        recording: discord.Attachment,
        plan_type: str | None,
    ) -> None:
        """Entry point for `/oc_talk` — transcribe an uploaded audio or video file.

        Like `/oc_voice`, but the audio comes from a Discord file attachment
        (``recording``) instead of a live Pycord recording session. Accepts
        audio files (mp3, wav, m4a, …) and video files (mov, mp4, avi, …) —
        for video, ffmpeg extracts the audio track before transcription.

        Ordering: validate → defer → create opencode session + text channel →
        post a "transcribing…" status message in the new channel + reply to the
        slash invoker with the channel pointer (so the user has the control
        channel immediately) → THEN download + transcribe the audio → edit the
        status message in place to show the transcript → send the transcript to
        the ``plan-author`` subagent → drive the session to completion (no live
        voice channel join, no TTS playback — text-only responses).
        """
        if not self._channel_ok(ctx.channel_id):
            await _deny(ctx)
            return
        guild = ctx.guild
        if guild is None:
            await ctx.respond("This command can only be used inside a server (guild).")
            return

        # Validate the attachment before deferring so the user gets an
        # immediate, non-ephemeral rejection message (defer hides replies
        # behind a "thinking…" state for up to 15min).
        if not is_transcribable_attachment(recording):
            await ctx.respond(
                f"That attachment (`{recording.filename}`) doesn't look like "
                f"audio or video. `/oc_talk` accepts mp3, wav, m4a, ogg, opus, "
                f"webm, flac, mov, mp4, avi, mkv."
            )
            return
        # No bot-side size cap: Discord's own per-server upload limit is the
        # only gate. Oversized attachments are rejected by Discord at the
        # slash-command invocation step before the bot sees them.

        await ctx.defer()

        # Create the opencode session + text channel (mirrors _run_prompt /
        # _start_voice_session). Title/reason mention /oc_talk for audit trail.
        try:
            session = await self.client.create_session(title="discord-talk")
        except OpencodeError as e:
            await ctx.followup.send(f"Failed to create opencode session: {e}")
            return
        sid = session["id"]
        short_sid = sid[:8] if sid else "unknown"
        slug = _slugify_prompt("talk-session", fallback=f"oc-talk-{short_sid}")

        category: discord.CategoryChannel | None = None
        cat_id = config.discord_bot_session_category_id
        if cat_id:
            resolved = guild.get_channel(cat_id)
            if isinstance(resolved, discord.CategoryChannel):
                category = resolved

        try:
            new_channel = await guild.create_text_channel(
                name=slug,
                category=category,
                topic=f"opencode talk session {sid} (started by {ctx.user})",
                reason=f"/oc_talk slash command by {ctx.user}",
            )
        except discord.HTTPException as e:
            await ctx.followup.send(f"Failed to create session channel `{slug}`: {e}")
            return
        await self.router.bind(new_channel.id, sid)

        # Post a "transcribing" status message in the new channel AND reply to
        # the slash invoker with the channel pointer BEFORE the transcription
        # runs. This way the user sees the new channel + a processing message
        # and has the control channel immediately, while the (potentially
        # multi-second) transcription runs in the background.
        status_msg = await new_channel.send(
            f"Transcribing `{recording.filename}` ({recording.size / 1_000_000:.1f}MB)…"
        )
        await ctx.followup.send(
            f"Created {new_channel.mention} — transcribing "
            f"{recording.size / 1_000_000:.1f}MB of audio from "
            f"`{recording.filename}`. Continuing in the text channel."
        )

        # Download + transcribe. Both can fail (ffmpeg missing, Whisper key
        # unset, corrupt media); surface the error in the new channel (which
        # the user already has a pointer to) and leave the channel bound so
        # the user can follow up with text.
        try:
            media_bytes = await recording.read()
            wav_bytes = await extract_audio_to_wav(
                media_bytes,
                content_type=recording.content_type,
                filename=recording.filename,
            )
            transcript = await transcribe_audio(wav_bytes)
        except Exception as e:  # noqa: BLE001 — transcription failures are user-facing
            _log.warning("/oc_talk transcription failed: %r", e)
            await status_msg.edit(
                content=f"Failed to transcribe `{recording.filename}`: {e}. "
                f"The session is bound to this channel — type your plan as "
                f"text and it will be forwarded to the same opencode session."
            )
            return

        transcript = (transcript or "").strip()
        if not transcript:
            await status_msg.edit(
                content="Transcription came back empty (no speech detected). "
                "The session is bound to this channel — type your plan as "
                "text to continue."
            )
            return

        # Best-effort LLM slug upgrade from the now-available transcript. The
        # text channel was created with the `talk-session` regex slug (see
        # `_run_talk_session`'s channel-creation block above); now that the
        # transcript exists, generate a real slug from it and rename. Fire-
        # and-forget so it doesn't delay the opencode prompt send. `fallback=
        # new_channel.name` keeps the initial name if the LLM call fails.
        asyncio.create_task(
            self._rename_when_slug_ready(
                new_channel, transcript, fallback=new_channel.name
            )
        )

        # Build the prompt with the optional plan-type directive (mirrors
        # _finalize_voice_session's directive logic for /oc_voice).
        prompt = transcript
        if plan_type is not None:
            directive = (
                "[PLAN_TYPE_PRESELECTED: actionable]"
                if plan_type == "actionable"
                else "[PLAN_TYPE_PRESELECTED: note]"
            )
            prompt = f"{directive}\n\n{transcript}"

        parts = [{"type": "text", "text": prompt}]
        try:
            await self.client.send_prompt_async(sid, parts, agent="plan-author")
        except OpencodeError as e:
            await new_channel.send(f"Failed to send prompt to session `{sid}`: {e}")
            return

        # Replace the "transcribing…" status with the transcript, then start
        # the opencode progress loop.
        await status_msg.edit(
            content=f"**Transcribed prompt:**\n```\n{transcript}\n```"
        )
        progress_msg = await new_channel.send(f"Working on session `{sid}`…")
        await self._drive_session(
            sid,
            send_chunk=new_channel.send,
            progress_msg=progress_msg,
            voice_session=None,  # no live voice path for /oc_talk
        )

    async def _run_talk_from_message(
        self,
        message: discord.Message,
        attachment: discord.Attachment,
    ) -> None:
        """Transcribe a voice-message attachment posted in the trigger channel
        and start a new plan-author session (the voice-in analog of
        ``_run_talk_session``, but triggered by a plain message instead of a
        slash command).

        Mirrors ``_run_talk_session`` but adapted for a plain-message trigger
        instead of a slash ``ApplicationContext``: there is no ``ctx.defer()``
        / ``ctx.followup.send``. The "created #channel" pointer is posted into
        the trigger channel (``message.channel``) via ``message.channel.send``;
        the status/progress messages go into the new session channel via
        ``new_channel.send``. No ``plan_type`` directive is sent (a plain
        message has no slash-option UI), so the plan-author agent classifies
        the transcript on its own.

        Flow: resolve guild → create opencode session → ``_slugify_prompt``
        → resolve category → create text channel + ``router.bind`` → post
        pointer into the trigger channel → post "Transcribing…" status in the
        new channel → download + ``extract_audio_to_wav`` + ``transcribe_audio``
        (try/except edits the status on failure) → empty-transcript guard →
        fire-and-forget LLM slug rename → send transcript to plan-author →
        edit status to show transcript → drive the session.
        """
        guild = message.guild
        if guild is None:
            # DMs or non-guild contexts: no channel to create under; ignore.
            return

        # Create the opencode session first so we can use its id as a slug
        # fallback and in the channel topic.
        try:
            session = await self.client.create_session(title="discord-voice-message")
        except OpencodeError as e:
            try:
                await message.channel.send(f"Failed to create opencode session: {e}")
            except discord.HTTPException:
                _log.warning(
                    "failed to post voice-message session-creation error: %r", e
                )
            return
        sid = session["id"]
        short_sid = sid[:8] if sid else "unknown"
        slug = _slugify_prompt("voice-message", fallback=f"oc-vm-{short_sid}")

        # Resolve the target category (best-effort; create with no parent if
        # unset or not found). Duplicated from `_run_talk_session` (the
        # refactor to a shared helper is out of scope — it would touch three
        # working methods).
        category: discord.CategoryChannel | None = None
        cat_id = config.discord_bot_session_category_id
        if cat_id:
            resolved = guild.get_channel(cat_id)
            if isinstance(resolved, discord.CategoryChannel):
                category = resolved

        try:
            new_channel = await guild.create_text_channel(
                name=slug,
                category=category,
                topic=f"opencode voice-message session {sid} (started by {message.author})",
                reason=f"voice message by {message.author}",
            )
        except discord.HTTPException as e:
            try:
                await message.channel.send(
                    f"Failed to create session channel `{slug}`: {e}"
                )
            except discord.HTTPException:
                _log.warning("failed to post channel-creation error: %r", e)
            return
        await self.router.bind(new_channel.id, sid)

        # Post the pointer into the TRIGGER channel (replaces
        # `ctx.followup.send` since there's no slash interaction here).
        try:
            await message.channel.send(
                f"Created {new_channel.mention} — transcribing "
                f"{attachment.size / 1_000_000:.1f}MB of audio. "
                f"Continuing in the text channel."
            )
        except discord.HTTPException as e:
            _log.warning("failed to post voice-message trigger-channel pointer: %r", e)

        # Post a "transcribing" status in the new channel BEFORE the
        # transcription runs, so the user sees the channel + processing message
        # immediately.
        status_msg = await new_channel.send(
            f"Transcribing `{attachment.filename}` "
            f"({attachment.size / 1_000_000:.1f}MB)…"
        )

        # Download + transcribe. Both can fail (ffmpeg missing, Whisper key
        # unset, corrupt media); surface the error in the new channel (which
        # the user already has a pointer to) and leave the channel bound so
        # the user can follow up with text.
        try:
            media_bytes = await attachment.read()
            wav_bytes = await extract_audio_to_wav(
                media_bytes,
                content_type=attachment.content_type,
                filename=attachment.filename,
            )
            transcript = await transcribe_audio(wav_bytes)
        except Exception as e:  # noqa: BLE001 — transcription failures are user-facing
            _log.warning("voice-message transcription failed: %r", e)
            await status_msg.edit(
                content=f"Failed to transcribe `{attachment.filename}`: {e}. "
                f"The session is bound to this channel — type your plan as "
                f"text and it will be forwarded to the same opencode session."
            )
            return

        transcript = (transcript or "").strip()
        if not transcript:
            await status_msg.edit(
                content="Transcription came back empty (no speech detected). "
                "The session is bound to this channel — type your plan as "
                "text to continue."
            )
            return

        # Best-effort LLM slug upgrade from the now-available transcript.
        asyncio.create_task(
            self._rename_when_slug_ready(
                new_channel, transcript, fallback=new_channel.name
            )
        )

        # Send the transcript to the plan-author agent with no plan_type
        # directive (a plain message has no slash-option UI, so let the agent
        # classify from the transcript wording — matches /oc_talk with
        # plan_type=None).
        parts = [{"type": "text", "text": transcript}]
        try:
            await self.client.send_prompt_async(sid, parts, agent="plan-author")
        except OpencodeError as e:
            await new_channel.send(f"Failed to send prompt to session `{sid}`: {e}")
            return

        # Replace the "transcribing…" status with the transcript, then start
        # the opencode progress loop.
        await status_msg.edit(
            content=f"**Transcribed prompt:**\n```\n{transcript}\n```"
        )
        progress_msg = await new_channel.send(f"Working on session `{sid}`…")
        await self._drive_session(
            sid,
            send_chunk=new_channel.send,
            progress_msg=progress_msg,
            voice_session=None,  # no live voice path
        )


def _status_to_progress_text(status: SessionStatus) -> str:
    """Render a short, human-readable progress line from a session status dict.

    ``status`` comes from ``GET /session/status`` and has a ``type`` field
    of ``"idle"`` | ``"busy"`` | ``"retry"`` (see
    ``packages/schema/src/session-status-event.ts``). Returns "" for idle
    (no progress to show) and for unknown types.
    """
    stype = (status or {}).get("type") or ""
    if stype == "busy":
        return "busy…"
    if stype == "retry":
        attempt = status.get("attempt")
        message = status.get("message")
        if attempt is not None:
            base = f"retrying (attempt {attempt})"
        else:
            base = "retrying"
        return f"{base}: {message}" if message else base
    return ""


def _final_assistant_text(messages: list[dict]) -> str:
    """Extract the text of the last assistant message from list_messages output.

    `list_messages` returns ``[{"info": Message, "parts": [Part]}, ...]``. We
    want the last message whose `info.role == "assistant"` and whose parts
    contain text. Falls back to the last message with any text.
    """
    last_assistant: str | None = None
    last_any: str | None = None
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info") or {}
        parts = entry.get("parts") or []
        text = _extract_text(parts)
        if not text:
            continue
        last_any = text
        if info.get("role") == "assistant":
            last_assistant = text
    return last_assistant if last_assistant is not None else (last_any or "")
