"""OpencodeBot — the Discord command surface for the opencode control bot.

`OpencodeBot(discord.Bot)` owns a decorator-based slash-command surface
(`@self.slash_command` + `discord.Option`, no prefix commands) PLUS a
plain-text follow-up path via `on_message` (and an edit-to-revert path via
`on_message_edit`). Each `/oc` or `/oc_plan` invocation creates a fresh
opencode session AND a fresh Discord text channel under the configured
category (`discord_bot_session_category_id`), then posts the response there.
Any subsequent plain-text message in that session channel is forwarded to
the bound opencode session as a follow-up prompt. The bot ignores messages
in channels it did not create.

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

Run via `python -m opencode_discord_bot` (see `opencode_discord_bot/__main__.py`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord

from opencode_discord_bot.events import SessionStatus, poll_until_idle
from opencode_discord_bot.opencode_client import OpencodeClient, OpencodeError
from opencode_discord_bot.questions import poll_pending_requests
from opencode_discord_bot.session_router import SessionRouter
from opencode_discord_bot.slug import aclose_slug_client, generate_slug
from opencode_discord_bot.text_utils import (
    DISCORD_MSG_MAX,
    _CHANNEL_NAME_MAX,
    _extract_text,
    _final_assistant_text,
    _slugify_prompt,
    _split_message,
)
from opencode_discord_bot.voice import (
    VoiceSession,
    extract_audio_to_wav,
    is_transcribable_attachment,
    transcribe_audio,
)
from opencode_discord_bot.opencode_serve import OpencodeServe, _REPO_ROOT
from opencode_discord_bot.config import config, reload_config
from opencode_discord_bot.env_writer import update_env_file

_log = logging.getLogger("bot.commands")

# Throttle for editing the "working…" progress message. Pycord's
# Message.edit is rate-limited (~5 edits/5s per channel); editing on every SSE
# event (dozens/sec during tool execution) would trip 429s. ~2s is safe.
PROGRESS_EDIT_MIN_INTERVAL = 2.0

# Stale baked-in default for `voice_message_trigger_channel_id` (see
# `config.py:107`). The field's documented "0 = disabled" sentinel is the
# clean unset state, but `config.py` ships with this non-zero magic number
# pointing at a specific channel in the original dev server. For `/oc_setup`'s
# "is this bot already set up?" gate, BOTH the 0 sentinel and this stale
# baked-in default count as "unset" — a fresh guild has neither.
_STALE_DEFAULT_TRIGGER_CHANNEL_ID = 1533242090862149842

# Persistence file for the Comulytic bridge's SessionRouter (channel id ->
# opencode session id). MUST match `bridge.py:_BRIDGE_SESSIONS_FILE` — the
# bridge owns writes to this file; the main bot only READS it (and `reset`s
# entries on `/oc_new` / `/oc_cleanup`). Kept as a string constant here
# rather than imported from `bridge.py` to avoid pulling the bridge's full
# import surface (comulytic client, etc.) into the main bot's module top.
_BRIDGE_SESSIONS_FILE = ".opencode-discord-bridge-sessions.json"


def _log_preview(text: str, max_len: int = 80) -> str:
    """Collapse whitespace and truncate ``text`` for safe one-line log output."""
    flat = " ".join(text.split())
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1] + "…"


def _channel_allowed(channel_id: int) -> bool:
    allow = config.discord_bot_allowed_channel_ids
    if not allow:
        return True
    return channel_id in allow


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "This channel is not allowed for the opencode bot.", ephemeral=True
    )


class OpencodeBot(discord.Bot):
    """Discord bot backing the opencode control bot slash commands.

    Owns the `opencode serve` subprocess lifecycle: `on_connect` starts the
    server (so the bot's `OpencodeClient` has something to talk to by the time
    it handles the first slash command), and `close` stops it. The password the
    server is spawned with is also seeded into this process's `os.environ` so
    the spawned subprocess inherits it; `OpencodeClient._auth()` reads
    `config.opencode_server_password` first and falls back to that env var.

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
        # Handle for the event-loop watchdog task (spawned at the end of
        # `on_connect`, cancelled in `close()`). A periodic `_log.info` every
        # 60s that records "event loop alive, t=<seconds>". If the loop freezes
        # (a synchronous call blocks asyncio), this stops printing — an instant
        # visible signal instead of a silent stall where the bot looks alive
        # (process running) but never heartbeats the gateway or dispatches
        # slash-command interactions (the "The application did not respond"
        # failure mode). See the PIPE→DEVNULL fix in `opencode_serve.py` for
        # the deadlock this watchdog was added to catch.
        self._watchdog_task: asyncio.Task | None = None
        # Handle for the optional in-process Comulytic bridge task (spawned in
        # `on_connect` when `config.comulytic_enabled` + `config.comulytic_jwt`
        # are set, cancelled + drained in `close()`). None when the bridge is
        # disabled or not yet started; guards against a reconnect re-spawn
        # (mirrors `_serve_started`).
        self._bridge_task: asyncio.Task | None = None
        # Lazy handle for the Comulytic bridge's SessionRouter (separate
        # persistence file `.opencode-discord-bridge-sessions.json` — see
        # `_BRIDGE_SESSIONS_FILE` above). Constructed on first
        # `_bridged_sid` call so deployments without the bridge never touch
        # the file. The bridge OWNS writes to this file (via its own
        # `SessionRouter` in `bridge.py:run_bridge`); the main bot only
        # reads it (and `reset`s entries on `/oc_new` / `/oc_cleanup`) so
        # the two processes don't clobber each other's writes.
        self.bridge_router: SessionRouter | None = None
        self._register_commands()

    def _register_commands(self) -> None:
        # `guild_ids` MUST match what `sync_commands.py` pushes to Discord. If
        # the bot registers commands with `guild_ids=None` (global) but
        # `sync_commands.py` pushes them to a specific guild, Discord sends
        # interactions with `guild_id` set, but Pycord's
        # `process_application_commands` name-fallback requires
        # `guild_id == cmd.guild_ids` — `None` never matches a real guild id,
        # so the interaction silently fails to match, no callback fires, no
        # `defer` is sent, and Discord shows "The application did not respond".
        # Passing `[guild_id]` here keeps the bot's in-memory command map
        # consistent with the guild-scoped registration `sync_commands.py`
        # pushed. When `discord_bot_guild_id` is 0 (unset), fall back to None
        # (global) — matching a global sync.
        _guild_ids = (
            [config.discord_bot_guild_id] if config.discord_bot_guild_id else None
        )

        @self.slash_command(
            name="oc",
            description="Send a prompt to opencode and wait for the response.",
            guild_ids=_guild_ids,
        )
        async def oc(
            ctx: discord.ApplicationContext,
            prompt: str = discord.Option(str, "The prompt to send to opencode."),
        ) -> None:
            _log.info(
                "/oc received (user=%s, channel=%s, guild=%s): %s",
                ctx.author,
                ctx.channel_id,
                ctx.guild_id,
                _log_preview(prompt),
            )
            await self._run_prompt(ctx, prompt, agent=None)

        @self.slash_command(
            name="oc_plan",
            description="Send a change to opencode's plan-author subagent (reasoned, plan-only).",
            guild_ids=_guild_ids,
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
            _log.info(
                "/oc_plan received (user=%s, channel=%s, guild=%s, plan_type=%s): %s",
                ctx.author,
                ctx.channel_id,
                ctx.guild_id,
                plan_type or "(auto-classify)",
                _log_preview(change),
            )
            await self._run_prompt(ctx, prompt, agent="plan-author", echo_prompt=change)

        @self.slash_command(
            name="oc_new",
            description="Reset this channel's opencode session (start fresh).",
            guild_ids=_guild_ids,
        )
        async def oc_new(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_new received (user=%s, channel=%s)", ctx.author, ctx.channel_id
            )
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            # Reset whichever router holds the binding. `reset` is a no-op
            # when the channel isn't bound, so resetting both is safe and
            # avoids branching on which router owns the channel — covers
            # both `/oc` / `/oc_plan` channels (main router) and
            # Comulytic-bridge channels (bridge router) so plain-text
            # follow-ups stop being forwarded in either case.
            await self.router.reset(ctx.channel_id)
            if self.bridge_router is not None:
                await self.bridge_router.reset(ctx.channel_id)
            await ctx.respond(
                f"Session binding reset for channel {ctx.channel_id}. "
                "Plain-text messages here will no longer be forwarded. "
                "Use `/oc` to create a new session channel."
            )

        @self.slash_command(
            name="oc_session",
            description="Show this channel's current opencode session + status.",
            guild_ids=_guild_ids,
        )
        async def oc_session(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_session received (user=%s, channel=%s)", ctx.author, ctx.channel_id
            )
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            await ctx.defer()
            sid = self._resolve_sid(ctx.channel_id)
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
            _log.info(
                "/oc_session -> session=%s title=%r status=%s", sid, title, status
            )
            await ctx.followup.send(
                f"Session `{sid}`\nTitle: {title}\nStatus: {status}"
            )

        @self.slash_command(
            name="oc_sessions",
            description="List recent opencode sessions.",
            guild_ids=_guild_ids,
        )
        async def oc_sessions(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_sessions received (user=%s, channel=%s)",
                ctx.author,
                ctx.channel_id,
            )
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
            _log.info("/oc_sessions -> %d total, %d recent", len(sessions), len(recent))
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
            guild_ids=_guild_ids,
        )
        async def oc_abort(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_abort received (user=%s, channel=%s)", ctx.author, ctx.channel_id
            )
            if not self._channel_ok(ctx.channel_id):
                await _deny(ctx)
                return
            await ctx.defer()
            sid = self._resolve_sid(ctx.channel_id)
            if sid is None:
                await ctx.followup.send("No opencode session bound to this channel.")
                return
            _log.info("/oc_abort -> aborting session=%s", sid)
            try:
                ok = await self.client.abort_session(sid)
            except OpencodeError as e:
                await ctx.followup.send(f"Failed to abort session `{sid}`: {e}")
                return
            _log.info("/oc_abort -> session=%s result=%s", sid, ok)
            await ctx.followup.send(f"Abort requested for session `{sid}`: {ok}.")

        @self.slash_command(
            name="oc_voice",
            description="Join a voice channel, transcribe your spoken plan/note, route to plan-author.",
            guild_ids=_guild_ids,
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
            _log.info(
                "/oc_voice received (user=%s, channel=%s, guild=%s, mode=%s)",
                ctx.author,
                ctx.channel_id,
                ctx.guild_id,
                mode,
            )
            await self._start_voice_session(ctx, mode, voice_channel)

        @self.slash_command(
            name="oc_voice_stop",
            description="Manually stop the active voice session and process the transcript.",
            guild_ids=_guild_ids,
        )
        async def oc_voice_stop(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_voice_stop received (user=%s, guild=%s)",
                ctx.author,
                ctx.guild_id,
            )
            session = self._voice_sessions.get(ctx.guild_id)
            if session is None:
                await ctx.respond("No active voice session in this guild.")
                return
            await ctx.defer()
            transcript = await session.stop()
            _log.info(
                "/oc_voice_stop -> voice session stopped (guild=%s, transcript_len=%d)",
                ctx.guild_id,
                len(transcript or ""),
            )
            await self._finalize_voice_session(ctx, session, transcript)

        @self.slash_command(
            name="oc_talk",
            description="Upload an audio/video recording of your thoughts; extract audio, transcribe, route to plan-author.",
            guild_ids=_guild_ids,
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
            _log.info(
                "/oc_talk received (user=%s, channel=%s, guild=%s, file=%s, "
                "size=%dB, plan_type=%s)",
                ctx.author,
                ctx.channel_id,
                ctx.guild_id,
                recording.filename,
                recording.size,
                plan_type or "(auto-classify)",
            )
            await self._run_talk_session(ctx, recording, plan_type)

        @self.slash_command(
            name="oc_cleanup",
            description="Delete all bot-created session channels in the session category.",
            guild_ids=_guild_ids,
        )
        async def oc_cleanup(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_cleanup received (user=%s, channel=%s, guild=%s)",
                ctx.author,
                ctx.channel_id,
                ctx.guild_id,
            )
            # Defer (ephemeral) because deleting many channels can take a few
            # seconds and we need to keep the interaction alive. Ephemeral so
            # the summary is only visible to the caller, not the whole guild.
            await ctx.defer(ephemeral=True)
            # Permission gate: bulk channel deletion is destructive, so it's
            # restricted to members with the Manage Channels permission (guild
            # admins and anyone granted the permission). A typo or stray
            # invocation by a non-admin must not be able to wipe channels.
            if not ctx.author.guild_permissions.manage_channels:
                await ctx.followup.send(
                    "You need the Manage Channels permission to use this command.",
                    ephemeral=True,
                )
                return
            # Resolve the session category. 0/unset = the bot was never
            # configured to create session channels under a category, so there
            # is nothing the bot created that this command should touch.
            category_id = config.discord_bot_session_category_id
            if category_id == 0:
                await ctx.followup.send(
                    "No session category is configured "
                    "(`DISCORD_BOT_SESSION_CATEGORY_ID` is 0). Nothing to clean up."
                )
                return
            category = ctx.guild.get_channel(category_id)
            if category is None:
                await ctx.followup.send(
                    f"Session category {category_id} not found in this guild. "
                    "Set `DISCORD_BOT_SESSION_CATEGORY_ID` to an existing category."
                )
                return
            if not isinstance(category, discord.CategoryChannel):
                # The configured id points at a non-category channel (e.g. a
                # text or voice channel); iterating `.text_channels` on it would
                # raise AttributeError, so fail with a clear error instead.
                await ctx.followup.send(
                    f"Channel {category_id} is not a category "
                    f"(got {type(category).__name__}). "
                    "Set `DISCORD_BOT_SESSION_CATEGORY_ID` to a category channel."
                )
                return
            # Snapshot the targets before deleting so the loop iterates over a
            # stable list (deletion mutates `category.text_channels` in place).
            # Category-scoped iteration is the safety mechanism: only channels
            # the bot created under this category are ever enumerated, so
            # allowlisted command channels, voice channels, and user-created
            # channels are never at risk.
            targets = list(category.text_channels)
            if not targets:
                await ctx.followup.send("No session channels to clean up.")
                return
            deleted = 0
            failed: list[str] = []
            # Clear the SessionRouter binding for each deleted channel so the
            # bot doesn't keep stale `channel_id -> session_id` mappings pointing
            # at now-deleted channels (which would make `_channel_ok` think a
            # follow-up has a live session and try to route into a deleted
            # channel). `reset` drops the binding and persists — it's the
            # existing `unbind` equivalent, so no new SessionRouter method is
            # needed. Both routers are cleared per channel: the main bot's
            # (`self.router`) for `/oc` / `/oc_plan` channels and the
            # Comulytic bridge's (`self.bridge_router`, if constructed) for
            # bridge-created channels, since `/oc_cleanup` deletes every
            # text channel under the session category regardless of which
            # router bound it. Wrapped per-channel so one router failure
            # doesn't lose the deletion summary; the router-clearing as a
            # whole is best-effort and never blocks the channel deletion.
            for ch in targets:
                try:
                    await ch.delete(reason=f"/oc_cleanup by {ctx.author}")
                    deleted += 1
                except discord.HTTPException as e:
                    failed.append(f"#{ch.name}: {e}")
                try:
                    if self.router.current(ch.id) is not None:
                        await self.router.reset(ch.id)
                    if self.bridge_router is not None and self.bridge_router.current(ch.id) is not None:
                        await self.bridge_router.reset(ch.id)
                except (
                    Exception
                ):  # noqa: BLE001 — router cleanup must not lose the summary
                    _log.warning(
                        "router reset for channel %s raised during /oc_cleanup",
                        ch.id,
                        exc_info=True,
                    )
            summary = f"Deleted {deleted}/{len(targets)} session channels."
            if failed:
                summary += "\nFailed:\n" + "\n".join(failed)
            _log.info(
                "/oc_cleanup -> deleted=%d/%d, failed=%d",
                deleted,
                len(targets),
                len(failed),
            )
            await ctx.followup.send(summary)

        @self.slash_command(
            name="oc_setup",
            description="One-time setup: create the sessions category + bot channels and persist their IDs to .env.",
            guild_ids=_guild_ids,
        )
        async def oc_setup(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_setup received (user=%s, channel=%s, guild=%s)",
                ctx.author,
                ctx.channel_id,
                ctx.guild_id,
            )
            await self._run_setup(ctx)

        @self.slash_command(
            name="oc_help",
            description="List the opencode bot commands.",
            guild_ids=_guild_ids,
        )
        async def oc_help(ctx: discord.ApplicationContext) -> None:
            _log.info(
                "/oc_help received (user=%s, channel=%s)", ctx.author, ctx.channel_id
            )
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
                "/oc_cleanup — delete all bot-created session channels in the "
                "session category. Requires the Manage Channels permission. "
                "Use to clean up the server between test sessions.\n"
                "/oc_setup — one-time setup. Creates the 'OpenCode Sessions' "
                "category plus the voice-recordings and bot-commands channels, "
                "writes their IDs to .env, and restricts slash commands to the "
                "bot-commands channel. Only runs if no guild-specific settings "
                "are configured yet. Requires the Manage Channels permission.\n"
                "/oc_help — this message.\n\n"
                "**Follow-ups:** in a channel created by `/oc` or `/oc_plan`, just type a "
                "plain-text message — it's forwarded to that channel's opencode session. "
                "One follow-up at a time (a busy session replies with a wait notice)."
            )
            await ctx.respond(text)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Delegate to Pycord's dispatcher, which routes application-command
        # interactions to the matching `@self.slash_command` callback and
        # component interactions (buttons/select menus from `questions.py`)
        # to their View's `callback`. The dispatcher is a no-op for unknown
        # interaction ids, which is fine — silently dropping a stray
        # component id is better than failing the whole interaction.
        await self.process_application_commands(interaction)

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
        this process's `os.environ` so the spawned subprocess inherits it.
        `OpencodeClient._auth()` reads `config.opencode_server_password`
        first (loaded from `.env` at import) and falls back to this env
        var, so the client and server share the same password regardless
        of which path populated it.
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

        # Auto-spawn the Comulytic bridge as an in-process task, gated on
        # config. Calls `run_bridge()` directly (NOT `main()` — `main()` calls
        # `asyncio.run(...)` which creates a new event loop, fatal inside the
        # bot's running loop, and reconfigures the root logger via
        # `logging.basicConfig`). The bridge's own `OpencodeServe.start()`
        # probe-healthy short-circuits (`_reused=True`, `stop()` no-op) so it
        # reuses the already-running opencode serve — no second subprocess, no
        # port conflict. Lifecycle is tied to `close()` which cancels + drains
        # the task so the bridge's `finally` (saving seen-set, closing clients,
        # no-op'ing its reused serve.stop()) runs BEFORE the bot kills the real
        # opencode serve subprocess. A bridge crash is isolated by the
        # `_bridge_guard` wrapper — it logs and dies without taking the bot
        # down. The `self._bridge_task is None` guard prevents a reconnect
        # re-spawn (mirrors `_serve_started`).
        if (
            config.comulytic_enabled
            and config.comulytic_jwt
            and self._bridge_task is None
        ):
            from opencode_discord_bot.bridge import run_bridge

            async def _bridge_guard() -> None:
                try:
                    await run_bridge()
                except asyncio.CancelledError:
                    raise  # close() cancels cleanly — let finally in run_bridge run
                except Exception:  # noqa: BLE001 — bridge crash must not kill the bot
                    _log.exception("comulytic bridge task crashed")

            self._bridge_task = asyncio.create_task(_bridge_guard())
            _log.info("comulytic bridge auto-started (in-process task)")

        # Event-loop watchdog: log "event loop alive" every 60s. If the loop
        # freezes (a blocking call stalls asyncio), this stops printing — a
        # visible signal instead of a silent stall. Guarded by `is None` so a
        # reconnect re-spawn doesn't create a second watchdog. Cancelled in
        # `close()` before the bridge + serve teardown.
        if self._watchdog_task is None:
            watchdog_start = time.monotonic()

            async def _event_loop_watchdog() -> None:
                try:
                    while True:
                        await asyncio.sleep(60.0)
                        _log.info(
                            "event loop alive, t=%.0fs",
                            time.monotonic() - watchdog_start,
                        )
                except asyncio.CancelledError:
                    raise  # close() cancels cleanly
                except Exception:  # noqa: BLE001 — watchdog must not kill the bot
                    _log.exception("event-loop watchdog crashed")

            self._watchdog_task = asyncio.create_task(_event_loop_watchdog())

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
        # Cancel + drain the comulytic bridge task FIRST so its `run_bridge()`
        # `finally` block runs (saves the seen-set, closes its httpx clients,
        # and no-ops its reused `serve.stop()`) BEFORE we kill the real opencode
        # serve subprocess below. A 10s drain ceiling so a stuck poll cycle
        # can't hang shutdown; on timeout we give up and the task is abandoned
        # (the process is exiting anyway, so orphaned clients are reaped by the
        # OS). `CancelledError` is expected and swallowed.
        # Cancel the event-loop watchdog FIRST so it doesn't log "event loop
        # alive" during the shutdown sequence (which would be noise). Drain it
        # with a short ceiling so a stuck sleep can't hang shutdown.
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._watchdog_task = None
        if self._bridge_task is not None:
            self._bridge_task.cancel()
            try:
                await asyncio.wait_for(self._bridge_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._bridge_task = None
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
        if self._resolve_sid(channel_id) is not None:
            return True
        return _channel_allowed(channel_id)

    def _bridged_sid(self, channel_id: int) -> str | None:
        """Return the opencode session id bound to `channel_id` in the
        Comulytic bridge's SessionRouter, or None.

        The bridge creates Discord channels for routed recordings and binds
        them in a SEPARATE persistence file
        (`.opencode-discord-bridge-sessions.json` — see `_BRIDGE_SESSIONS_FILE`)
        so its writes don't clobber the main bot's. Without consulting that
        file, the main bot's `on_message` / slash commands would be blind to
        bridge-created channels and a user returning to one hours later
        couldn't resume the session (their plain-text follow-ups would
        silently hit the "not a session channel" branch and be ignored).

        The bridge router is constructed lazily on first call so
        deployments without the bridge never open the file.
        `SessionRouter.__init__` swallows `OSError`/JSON errors and falls
        back to an empty map, so a missing/corrupt file degrades to "no
        bridge bindings" rather than crashing.
        """
        if self.bridge_router is None:
            self.bridge_router = SessionRouter(Path(_BRIDGE_SESSIONS_FILE))
        return self.bridge_router.current(channel_id)

    def _resolve_sid(self, channel_id: int) -> str | None:
        """Unified channel->session lookup across both routers.

        Returns the main bot's binding (`self.router`) if present, else the
        bridge's (`_bridged_sid`). The main bot's `SessionRouter` is
        authoritative for `/oc` and `/oc_plan` channels; the bridge's is
        authoritative for Comulytic-routed channels. A channel is only ever
        bound in one of the two files, so the order is unambiguous.
        """
        sid = self.router.current(channel_id)
        if sid is not None:
            return sid
        return self._bridged_sid(channel_id)

    def _is_guild_configured(self) -> bool:
        """True if ANY of `/oc_setup`'s *output* settings is already configured.

        `/oc_setup` is a one-time setup command — it refuses to run if any of
        the three guild-specific fields it *creates* already has a non-default
        value, to avoid silently overwriting a working setup (or creating a
        duplicate "OpenCode Sessions" category). The "unset" sentinels:

          - `discord_bot_session_category_id == 0` (config default)
          - `discord_bot_allowed_channel_ids` empty (config default)
          - `voice_message_trigger_channel_id` is 0 OR the stale baked-in
            default (`_STALE_DEFAULT_TRIGGER_CHANNEL_ID`) — the latter ships
            in `config.py:107` pointing at a channel in the original dev
            server, which a fresh guild doesn't have, so it counts as unset.

        If any one of these is non-default, the bot is considered "already
        set up" for this guild and `/oc_setup` refuses. The user must clear
        the fields in `.env` manually to re-run setup (intentionally
        destructive, so not exposed as a command toggle).

        `discord_bot_guild_id` is deliberately NOT part of this gate: it's an
        *input* to setup (read by `sync_commands.py` as the default sync
        target, and written-through by `/oc_setup` only if previously 0),
        not an output. `SETUP_GUIDE.md` tells users to set it in `.env`
        before running `/oc_setup`, and `sync_commands.py` needs it (or
        `--guild`) to push the `/oc_setup` command into the guild in the
        first place — gating on it would make the documented setup flow
        unrunnable (the bot would refuse with an ephemeral "already set up"
        message the instant the user follows the guide).
        """
        if config.discord_bot_session_category_id != 0:
            return True
        if config.discord_bot_allowed_channel_ids:
            return True
        if config.voice_message_trigger_channel_id not in (
            0,
            _STALE_DEFAULT_TRIGGER_CHANNEL_ID,
        ):
            return True
        return False

    async def _run_setup(self, ctx: discord.ApplicationContext) -> None:
        """`/oc_setup` — one-time guild setup.

        Refuses if any guild-specific setting is already configured (see
        `_is_guild_configured`). Otherwise:

        1. Defers ephemerally (channel/category creation + `.env` write can
           take a couple seconds).
        2. Requires the Manage Channels permission (mirrors `/oc_cleanup`).
        3. Creates a category named "OpenCode Sessions" in the invoking guild.
        4. Creates two text channels at guild ROOT (NOT under the new
           category) so `/oc_cleanup` — which deletes every text channel
           under `discord_bot_session_category_id` — won't wipe them:
           - `voice-recordings` — repurposed as `VOICE_MESSAGE_TRIGGER_CHANNEL_ID`
             (voice messages posted there start new plan-author sessions).
           - `bot-commands` — repurposed as `DISCORD_BOT_ALLOWED_CHANNEL_IDS`
             (a one-element JSON list so slash commands are restricted to
             this channel + bot-created session channels).
        5. Writes the three created IDs to `.env` (cwd-relative, matching
           `BotConfig.model_config`'s `env_file=".env"`) via `env_writer`,
           so they persist across restarts. Also writes `DISCORD_BOT_GUILD_ID`
           from `ctx.guild.id` if it was previously unset.
        6. Reloads the `config` singleton in place so the running bot sees
           the new IDs immediately (no restart needed).
        7. Replies with a summary of what was created + the IDs, and a
           reminder to run `python -m opencode_discord_bot.sync_commands
           --guild <id>` to (re)sync the slash-command surface (the bot
           has `auto_sync_commands=False`, so `/oc_setup` itself won't be
           available until the next sync).

        On any `discord.HTTPException` during category/channel creation,
        replies with an ephemeral error and does NOT write `.env` (no
        half-state).
        """
        await ctx.defer(ephemeral=True)
        # Already-setup gate: refuse if any guild-specific field is set, so a
        # re-invocation can't silently overwrite a working setup or create a
        # duplicate category. Ephemeral so only the caller sees the refusal.
        if self._is_guild_configured():
            await ctx.followup.send(
                "Setup has already been run (at least one guild-specific "
                "setting is configured). To re-run, clear these in `.env` "
                "manually first:\n"
                "- `DISCORD_BOT_SESSION_CATEGORY_ID` "
                f"(currently `{config.discord_bot_session_category_id}`)\n"
                "- `DISCORD_BOT_ALLOWED_CHANNEL_IDS` "
                f"(currently `{config.discord_bot_allowed_channel_ids}`)\n"
                "- `VOICE_MESSAGE_TRIGGER_CHANNEL_ID` "
                f"(currently `{config.voice_message_trigger_channel_id}`)",
                ephemeral=True,
            )
            return
        guild = ctx.guild
        if guild is None:
            await ctx.followup.send(
                "This command can only be used inside a server (guild).",
                ephemeral=True,
            )
            return
        # Permission gate: creating a category + channels is guild-modifying,
        # so restrict to members with Manage Channels (mirrors `/oc_cleanup`).
        if not ctx.author.guild_permissions.manage_channels:
            await ctx.followup.send(
                "You need the Manage Channels permission to use this command.",
                ephemeral=True,
            )
            return

        # Create the category first so the summary can show its id even if
        # channel creation later fails (the category is the primary artifact
        # the user named in the request).
        try:
            category = await guild.create_category(
                "OpenCode Sessions",
                reason=f"/oc_setup by {ctx.author}",
            )
        except discord.HTTPException as e:
            await ctx.followup.send(
                f"Failed to create 'OpenCode Sessions' category: {e}",
                ephemeral=True,
            )
            return

        # Create the two channels at guild ROOT (no `category=` kwarg) so
        # `/oc_cleanup` — which deletes every text channel under
        # `discord_bot_session_category_id` — won't wipe them on a cleanup
        # pass. Both channels are plain text channels (type 0); Discord
        # voice channels would be type 2 and aren't needed here (the bot's
        # `/oc_voice` joins the user's current voice channel, not a bot-owned
        # one).
        try:
            recordings_ch = await guild.create_text_channel(
                "voice-recordings",
                reason=f"/oc_setup by {ctx.author} (voice-message trigger channel)",
            )
        except discord.HTTPException as e:
            await ctx.followup.send(
                f"Created category 'OpenCode Sessions' (id `{category.id}`) "
                f"but failed to create the voice-recordings channel: {e}. "
                f"The category was left in place — delete it manually if you "
                f"want to re-run setup.",
                ephemeral=True,
            )
            return
        try:
            commands_ch = await guild.create_text_channel(
                "bot-commands",
                reason=f"/oc_setup by {ctx.author} (bot commands allowlist)",
            )
        except discord.HTTPException as e:
            await ctx.followup.send(
                f"Created category 'OpenCode Sessions' (id `{category.id}`) "
                f"and channel #voice-recordings (id `{recordings_ch.id}`), "
                f"but failed to create the bot-commands channel: {e}. "
                f"The category + voice-recordings channel were left in place "
                f"— delete them manually if you want to re-run setup.",
                ephemeral=True,
            )
            return

        # Build the `.env` updates. Keys not present in the file are appended
        # at the end; present keys have their values replaced in place (see
        # `env_writer.update_env_file`). `DISCORD_BOT_ALLOWED_CHANNEL_IDS` is
        # a JSON list per pydantic-settings v2 parsing (e.g. `[123,456]`).
        env_path = Path.cwd() / ".env"
        updates: dict[str, str] = {
            "DISCORD_BOT_SESSION_CATEGORY_ID": str(category.id),
            "VOICE_MESSAGE_TRIGGER_CHANNEL_ID": str(recordings_ch.id),
            "DISCORD_BOT_ALLOWED_CHANNEL_IDS": f"[{commands_ch.id}]",
        }
        if config.discord_bot_guild_id == 0:
            updates["DISCORD_BOT_GUILD_ID"] = str(guild.id)
        try:
            update_env_file(env_path, updates)
        except OSError as e:
            await ctx.followup.send(
                f"Created category + channels but failed to write `.env` at "
                f"`{env_path}`: {e}. The IDs were NOT persisted — copy them "
                f"manually:\n"
                f"- `DISCORD_BOT_SESSION_CATEGORY_ID={category.id}`\n"
                f"- `VOICE_MESSAGE_TRIGGER_CHANNEL_ID={recordings_ch.id}`\n"
                f"- `DISCORD_BOT_ALLOWED_CHANNEL_IDS=[{commands_ch.id}]`"
                + (
                    f"\n- `DISCORD_BOT_GUILD_ID={guild.id}`"
                    if "DISCORD_BOT_GUILD_ID" in updates
                    else ""
                ),
                ephemeral=True,
            )
            return

        # Reload the `config` singleton in place so the running bot sees
        # the new IDs immediately (no restart needed). `reload_config`
        # re-reads `.env` + env vars and mutates the existing singleton via
        # `__dict__.update`, so every module holding a reference to `config`
        # picks up the new values.
        try:
            reload_config()
        except Exception as e:  # noqa: BLE001 — reload must not kill the reply
            _log.warning("reload_config after /oc_setup raised: %r", e)

        # Summary reply (ephemeral). Includes the category id prominently
        # (the user asked for it to be displayed), the two channel ids +
        # their roles, and the guild-id write status. The sync reminder is
        # necessary because `auto_sync_commands=False` (see `__init__`)
        # means `/oc_setup` itself — and every other slash command — only
        # becomes available after a `sync_commands` push.
        guild_id_line = (
            f"- `DISCORD_BOT_GUILD_ID` set to `{guild.id}` (was previously unset)\n"
            if "DISCORD_BOT_GUILD_ID" in updates
            else f"- `DISCORD_BOT_GUILD_ID` left at `{config.discord_bot_guild_id}` (already set)\n"
        )
        await ctx.followup.send(
            "Setup complete. Created:\n"
            f"- Category **OpenCode Sessions** → id `{category.id}`\n"
            f"- Channel {recordings_ch.mention} → id `{recordings_ch.id}` "
            "(set as `VOICE_MESSAGE_TRIGGER_CHANNEL_ID` — voice messages "
            "posted here start new plan-author sessions)\n"
            f"- Channel {commands_ch.mention} → id `{commands_ch.id}` "
            "(set as `DISCORD_BOT_ALLOWED_CHANNEL_IDS` — slash commands now "
            "restricted to this channel + bot-created session channels)\n"
            + guild_id_line
            + "\nThese IDs were written to `.env` and reloaded into the "
            "running bot. They persist across restarts.\n\n"
            "**Next step:** run\n"
            f"```\npython -m opencode_discord_bot.sync_commands --guild {guild.id}\n```\n"
            "to push the slash-command surface (including `/oc_setup`) to "
            "this guild. Slash commands are not auto-synced on startup "
            "(`auto_sync_commands=False`).",
            ephemeral=True,
        )

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
        _log.info("driving session %s: polling status until idle", sid)
        # The request poller surfaces pending question/permission requests
        # for this session as Discord buttons/selects (or via voice when
        # `voice_session` is set). Runs concurrently with `poll_until_idle` and
        # is cancelled when the session goes idle (or times out). See
        # opencode_discord_bot.questions.poll_pending_requests.
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
                _log.info("session %s status: %s", sid, text)
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
                _log.info("session %s drive cancelled (abort or shutdown)", sid)
            else:
                _log.info("session %s went idle; fetching final messages", sid)

            try:
                messages = await self.client.list_messages(sid)
            except OpencodeError as e:
                _log.warning("session %s list_messages failed: %r", sid, e)
                await send_chunk(f"Failed to fetch final messages: {e}")
                return None
            final_text = _final_assistant_text(messages)
            if not final_text:
                _log.info("session %s produced no text output", sid)
                await send_chunk(f"Done (no text output). Session `{sid}` is now idle.")
                return None
            prefix = f"**opencode** (session `{sid}`):\n"
            chunks = list(_split_message(prefix + final_text))
            _log.info(
                "session %s final reply: %d chars in %d chunk(s)",
                sid,
                len(final_text),
                len(chunks),
            )
            for chunk in chunks:
                await send_chunk(chunk)
            return final_text
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(poller, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                poller.cancel()
            self._active_drives.pop(sid, None)
            _log.info("session %s drive finished", sid)

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
            _log.warning("create_session failed: %r", e)
            await ctx.followup.send(f"Failed to create opencode session: {e}")
            return
        sid = session["id"]
        short_sid = sid[:8] if sid else "unknown"
        slug = _slugify_prompt(prompt, fallback=f"oc-{short_sid}")
        _log.info(
            "created opencode session %s (agent=%s, prompt=%s)",
            sid,
            agent or "(default)",
            _log_preview(echo_prompt or prompt),
        )

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
            _log.warning("create_text_channel failed for session %s: %r", sid, e)
            await ctx.followup.send(f"Failed to create session channel `{slug}`: {e}")
            return
        _log.info(
            "created session channel #%s (id=%s) for session %s",
            new_channel.name,
            new_channel.id,
            sid,
        )

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
            _log.warning("send_prompt_async failed for session %s: %r", sid, e)
            await ctx.followup.send(
                f"Created {new_channel.mention} but failed to send prompt to session `{sid}`: {e}"
            )
            await new_channel.send(f"Failed to send initial prompt: {e}")
            return
        _log.info("prompt sent to session %s (agent=%s)", sid, agent or "(default)")

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
        # Unified lookup: consult the main bot's router first, then fall
        # back to the Comulytic bridge's router so a user returning to a
        # bridge-created channel hours later can resume the session with
        # a plain-text follow-up (the bridge's initial turn has long since
        # ended; the follow-up is driven by `_drive_session` here in the
        # main bot, which owns the gateway + button question poller).
        sid = self._resolve_sid(message.channel.id)
        voice_att = (
            self._voice_attachment(message) if config.voice_message_enabled else None
        )
        # (a) Voice follow-up in an existing session channel.
        if sid is not None and voice_att is not None:
            if sid in self._active_drives:
                _log.info(
                    "follow-up rejected (session %s busy): voice message in #%s",
                    sid,
                    message.channel.name,
                )
                await message.channel.send(
                    "Session is busy, please wait for the current response to finish."
                )
                return
            _log.info(
                "follow-up voice message in #%s -> session %s (file=%s, size=%dB)",
                message.channel.name,
                sid,
                voice_att.filename,
                voice_att.size,
            )
            await self._run_voice_followup(message, sid, voice_att)
            return
        # (b) Text follow-up in an existing session channel (existing path).
        if sid is not None and voice_att is None:
            if not message.content:
                return
            if sid in self._active_drives:
                _log.info(
                    "follow-up rejected (session %s busy): text in #%s",
                    sid,
                    message.channel.name,
                )
                await message.channel.send(
                    "Session is busy, please wait for the current response to finish."
                )
                return
            _log.info(
                "follow-up text in #%s -> session %s: %s",
                message.channel.name,
                sid,
                _log_preview(message.content),
            )
            await self._run_followup(message, sid)
            return
        # (c) Voice message in the configured trigger channel -> new session.
        if (
            sid is None
            and voice_att is not None
            and config.voice_message_trigger_channel_id
            and message.channel.id == config.voice_message_trigger_channel_id
        ):
            _log.info(
                "voice message in trigger channel #%s -> new plan-author session "
                "(file=%s, size=%dB)",
                message.channel.name,
                voice_att.filename,
                voice_att.size,
            )
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
            _log.warning(
                "follow-up send_prompt_async failed for session %s: %r", sid, e
            )
            await message.channel.send(
                f"Failed to send follow-up to session `{sid}`: {e}"
            )
            return
        _log.info("follow-up prompt sent to session %s", sid)

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
            _log.info(
                "voice follow-up transcript empty for session %s (no speech detected)",
                sid,
            )
            await status_msg.edit(
                content="Transcription came back empty (no speech detected). "
                "The session is bound to this channel — type your plan as "
                "text to continue."
            )
            return

        _log.info(
            "voice follow-up transcribed for session %s: %s",
            sid,
            _log_preview(transcript),
        )
        # Send the transcript to the session's existing agent (no override).
        parts = [{"type": "text", "text": transcript}]
        try:
            await self.client.send_prompt_async(sid, parts)
        except OpencodeError as e:
            _log.warning(
                "voice follow-up send_prompt_async failed for session %s: %r", sid, e
            )
            await message.channel.send(
                f"Failed to send voice follow-up to session `{sid}`: {e}"
            )
            return
        _log.info("voice follow-up prompt sent to session %s", sid)

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
        # (b) Not a session channel? (unified lookup — bridge channels too)
        sid = self._resolve_sid(after.channel.id)
        if sid is None:
            return
        # (c) No mapping for this Discord message?
        opencode_msg_id = self._prompt_msg_map.get(after.channel.id, {}).get(after.id)
        if opencode_msg_id is None:
            return
        # (d) Abort + cancel the running drive task if busy.
        _log.info(
            "edit-and-resend on #%s -> session %s (old=%s, new: %s)",
            after.channel.name,
            sid,
            opencode_msg_id,
            _log_preview(after.content),
        )
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
            _log.warning("edit-and-resend revert failed for session %s: %r", sid, e)
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
            _log.warning(
                "edit-and-resend send failed for session %s (revert succeeded): %r",
                sid,
                e,
            )
            try:
                await after.channel.send(
                    f"Edit-and-resend: revert succeeded but sending the new "
                    f"prompt failed: {e}."
                )
            except discord.HTTPException:
                _log.warning("failed to post edit-and-resend send error", exc_info=True)
            return
        _log.info("edit-and-resend prompt sent to session %s", sid)
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
