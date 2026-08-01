"""Discord UI surface for opencode's blocking ``question`` and ``permission`` requests.

The opencode server parks an agent turn on a deferred whenever the agent calls
the ``question`` tool (``packages/opencode/src/tool/question.ts``) or a tool
hits a permission rule whose action is ``"ask"`` (``packages/opencode/src/permission/index.ts``).
Session status stays ``"busy"`` the whole time — there is no "waiting for input"
status — so the bot's ``poll_until_idle`` loop can't tell a blocked turn from a
running one. This module polls ``GET /question`` and ``GET /permission`` for
the session being driven, renders each pending request as Discord buttons /
select menus, and POSTs the user's choice back to the matching REST endpoint
so the deferred resolves and the agent turn resumes.

Wire format reference (``packages/schema/src/v1/question.ts`` and
``.../v1/permission.ts``):

  Question.Request = { id, sessionID, questions: Question.Info[], tool? }
  Question.Info    = { question, header, options: Option[], multiple?, custom? }
  Question.Option  = { label, description }
  Question answer  = string[][]  (one array of selected labels per question, in order)

  Permission.Request = { id, sessionID, permission, patterns, metadata, always, tool? }
  Permission reply   = { reply: "once"|"always"|"reject", message? }

Discord component limits that shape this code:
  - Max 5 buttons per action row, 5 rows per View (25 buttons total).
  - Max 5 select menus per View (one per row).
  - A View holds either buttons OR select menus, not both in the same row.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord
from discord import ui

from opencode_discord_bot.opencode_client import OpencodeClient, OpencodeError

if TYPE_CHECKING:
    from opencode_discord_bot.voice import VoiceSession

_log = logging.getLogger("bot.questions")

# Poll interval for the pending-request loop. Matches the session-status poll
# cadence in bot.events.poll_until_idle so both loops stay in sync.
_POLL_INTERVAL = 2.0


class CustomAnswerModal(ui.Modal):
    """A single free-text input for a question with ``custom`` enabled.

    Stores the typed text via ``_on_submit`` so the owning View can fold it
    into the answer array in question order. The title is set per-question via
    the constructor (`super().__init__(title=...)`).
    """

    def __init__(
        self,
        title: str,
        on_submit: Callable[[str], Awaitable[None]],
    ) -> None:
        super().__init__(title=title[:45])
        self._on_submit = on_submit
        self.answer = ui.TextInput(
            label="Your answer",
            style=discord.InputTextStyle.paragraph,
            required=True,
            max_length=2000,
        )
        self.add_item(self.answer)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._on_submit(self.answer.value)
        await interaction.response.edit_message(view=None)


def _fmt_options(options: list[dict], limit: int = 10) -> str:
    """Render an option list as a numbered text block for the message body."""
    lines = []
    for i, opt in enumerate(options[:limit], 1):
        label = opt.get("label", "?")
        desc = opt.get("description", "")
        lines.append(f"**{i}.** {label}" + (f" — {desc}" if desc else ""))
    if len(options) > limit:
        lines.append(f"_(+{len(options) - limit} more, see menu)_")
    return "\n".join(lines)


def _question_block(info: dict) -> str:
    """Render one Question.Info as a header + options text block."""
    header = info.get("header", info.get("question", "Question"))
    multi = info.get("multiple") is True
    custom = info.get("custom") is not False
    out = [f"**{header}**"]
    if info.get("question"):
        out.append(f"> {info['question']}")
    if info.get("options"):
        out.append(_fmt_options(info["options"]))
    tags = []
    if multi:
        tags.append("multi-select")
    if custom:
        tags.append("custom allowed")
    if tags:
        out.append(f"_({', '.join(tags)})_")
    return "\n".join(out)


def _permission_block(request: dict) -> str:
    """Render a permission request as a header + patterns + metadata block."""
    perm = request.get("permission", "?")
    patterns = request.get("patterns") or []
    out = [f"**Permission: {perm}**"]
    if patterns:
        joined = ", ".join(f"`{p}`" for p in patterns)
        out.append(f"Matching: {joined}")
    meta = request.get("metadata") or {}
    if meta:
        meta_lines = [f"- `{k}`: {v}" for k, v in meta.items() if v is not None]
        if meta_lines:
            out.append("Metadata:\n" + "\n".join(meta_lines))
    if perm == "doom_loop":
        out.append(
            ":warning: The agent appears stuck in a loop. Approving lets it "
            "continue; rejecting cancels the looping tool."
        )
    return "\n".join(out)


class QuestionView(ui.View):
    """Renders one opencode question request as Discord buttons/selects.

    Handles the three shapes the opencode question tool produces:
      1. Single question, <=5 options, not multiple  -> one button per option.
      2. Single question, >5 options or multiple      -> one select menu.
      3. Multiple questions                            -> one select per question
         + a submit button (answers collected per-question, sent in order).

    ``custom`` (default true) adds a "Type your own answer" button that opens
    a ``CustomAnswerModal``; the typed text replaces that question's answer.

    The View is created with no Discord-side timeout (``timeout=None``); the
    poller cancels it via ``stop()`` when the session ends.
    """

    def __init__(
        self,
        request: dict,
        on_submit: Callable[[list[list[str]]], Awaitable[None]],
        on_reject: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self._request_id = request.get("id", "")
        self._questions: list[dict] = request.get("questions") or []
        self._on_submit = on_submit
        self._on_reject = on_reject
        # Per-question partial answers, keyed by question index. Each value is
        # the list of selected labels (one for single-pick, many for multi).
        self._answers: dict[int, list[str]] = {}
        self._done = False

        # Always-available reject button (last row).
        self.reject_btn = ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            row=4,
        )
        self.reject_btn.callback = self._on_reject_click
        self.add_item(self.reject_btn)

        if len(self._questions) <= 1:
            self._build_single()
        else:
            self._build_multi()

    # --- single-question layout ---

    def _build_single(self) -> None:
        if not self._questions:
            return
        info = self._questions[0]
        options = info.get("options") or []
        multiple = info.get("multiple") is True
        custom = info.get("custom") is not False

        if not multiple and len(options) <= 5:
            for i, opt in enumerate(options):
                btn = ui.Button(
                    label=opt.get("label", f"opt {i + 1}")[:80],
                    style=discord.ButtonStyle.primary,
                    row=0,
                    emoji=None,
                )

                def _cb(
                    _interaction: discord.Interaction,
                    _label: str = opt.get("label", ""),
                ) -> Any:
                    return self._submit_single(_label)

                btn.callback = _cb
                self.add_item(btn)
        else:
            select = ui.Select(
                placeholder=info.get("header", "Pick an option"),
                min_values=1,
                max_values=len(options) if multiple else 1,
                options=[
                    discord.SelectOption(
                        label=opt.get("label", f"opt {i + 1}")[:100],
                        description=(opt.get("description") or "")[:100],
                        value=opt.get("label", f"opt {i + 1}"),
                    )
                    for i, opt in enumerate(options)
                ],
                row=0,
            )
            select.callback = self._on_select_single
            self.add_item(select)

        if custom:
            custom_btn = ui.Button(
                label="Type your own answer",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            custom_btn.callback = self._on_custom_single
            self.add_item(custom_btn)

    async def _submit_single(self, label: str) -> None:
        if self._done:
            return
        self._done = True
        await self._on_submit([[label]])

    async def _on_select_single(self, interaction: discord.Interaction) -> None:
        if self._done:
            return
        select = self._get_select(0)
        if select is None:
            return
        await self._submit_multi_value(interaction, 0, select.values)

    async def _on_custom_single(self, interaction: discord.Interaction) -> None:
        if self._done:
            return
        info = self._questions[0] if self._questions else {}
        header = info.get("header", "Your answer")

        async def _on_modal(text: str) -> None:
            if self._done:
                return
            self._done = True
            await self._on_submit([[text]])

        await interaction.response.send_modal(CustomAnswerModal(header, _on_modal))

    # --- multi-question layout ---

    def _build_multi(self) -> None:
        # One select per question (rows 0..N-1), submit + custom buttons share
        # later rows. Discord caps at 5 rows; questions beyond 5 are dropped
        # with a warning (the agent rarely asks >5 in one call).
        max_questions = 5
        for idx, info in enumerate(self._questions[:max_questions]):
            options = info.get("options") or []
            multiple = info.get("multiple") is True
            select = ui.Select(
                placeholder=info.get("header", f"Q{idx + 1}"),
                min_values=1,
                max_values=len(options) if multiple else 1,
                options=[
                    discord.SelectOption(
                        label=opt.get("label", f"opt {j + 1}")[:100],
                        description=(opt.get("description") or "")[:100],
                        value=opt.get("label", f"opt {j + 1}"),
                    )
                    for j, opt in enumerate(options)
                ],
                row=idx,
            )

            def _cb(
                interaction: discord.Interaction,
                _idx: int = idx,
                _select: ui.Select = select,
            ) -> Any:
                return self._on_select_multi(interaction, _idx, _select)

            select.callback = _cb
            self.add_item(select)

            custom = info.get("custom") is not False
            if custom:
                custom_btn = ui.Button(
                    label=f"Custom Q{idx + 1}",
                    style=discord.ButtonStyle.secondary,
                    row=idx,
                )

                def _ccb(interaction: discord.Interaction, _idx: int = idx) -> Any:
                    return self._on_custom_multi(interaction, _idx)

                custom_btn.callback = _ccb
                self.add_item(custom_btn)

        if len(self._questions) > max_questions:
            _log.warning(
                "question request %s had %d questions; only first %d rendered",
                self._request_id,
                len(self._questions),
                max_questions,
            )

        self.submit_btn = ui.Button(
            label="Submit answers",
            style=discord.ButtonStyle.success,
            row=4,
            disabled=True,
        )
        self.submit_btn.callback = self._on_submit_multi
        self.add_item(self.submit_btn)

    async def _on_select_multi(
        self, interaction: discord.Interaction, idx: int, select: ui.Select
    ) -> None:
        await self._submit_multi_value(interaction, idx, select.values)

    async def _on_custom_multi(
        self, interaction: discord.Interaction, idx: int
    ) -> None:
        info = self._questions[idx] if idx < len(self._questions) else {}
        header = info.get("header", f"Q{idx + 1}")

        async def _on_modal(text: str) -> None:
            self._answers[idx] = [text]
            await self._refresh_submit_state(interaction)

        await interaction.response.send_modal(CustomAnswerModal(header, _on_modal))

    async def _submit_multi_value(
        self, interaction: discord.Interaction, idx: int, values: list[str]
    ) -> None:
        if not values:
            return
        self._answers[idx] = list(values)
        await self._refresh_submit_state(interaction)

    async def _refresh_submit_state(self, interaction: discord.Interaction) -> None:
        if hasattr(self, "submit_btn"):
            have_all = all(i in self._answers for i in range(len(self._questions[:5])))
            self.submit_btn.disabled = not have_all
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_submit_multi(self, interaction: discord.Interaction) -> None:
        if self._done:
            return
        if any(i not in self._answers for i in range(len(self._questions[:5]))):
            return
        self._done = True
        answers = [self._answers[i] for i in range(len(self._questions[:5]))]
        await self._on_submit(answers)

    async def _on_reject_click(self, interaction: discord.Interaction) -> None:
        if self._done:
            return
        self._done = True
        await self._on_reject()

    # --- helpers ---

    def _get_select(self, row: int) -> ui.Select | None:
        for child in self.children:
            if isinstance(child, ui.Select) and child.row == row:
                return child
        return None


class PermissionView(ui.View):
    """Renders one opencode permission request as three buttons.

    ``Allow once``  -> POST /permission/:id/reply {reply:"once"}
    ``Always allow`` -> POST /permission/:id/reply {reply:"always"}  (persists a rule)
    ``Reject``       -> POST /permission/:id/reply {reply:"reject"}  (fails the tool)

    The permission name, patterns, and metadata are in the message content
    (see ``_permission_block``); the View only carries the buttons.
    """

    def __init__(
        self,
        request: dict,
        on_reply: Callable[[str], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self._request_id = request.get("id", "")
        self._on_reply = on_reply
        self._done = False

        allow = ui.Button(label="Allow once", style=discord.ButtonStyle.success, row=0)
        always = ui.Button(
            label="Always allow",
            style=discord.ButtonStyle.success,
            row=0,
            emoji="\u26a0\ufe0f",
        )
        reject = ui.Button(label="Reject", style=discord.ButtonStyle.danger, row=0)

        allow.callback = lambda _i: self._reply("once")
        always.callback = lambda _i: self._reply("always")
        reject.callback = lambda _i: self._reply("reject")

        self.add_item(allow)
        self.add_item(always)
        self.add_item(reject)

    async def _reply(self, reply: str) -> None:
        if self._done:
            return
        self._done = True
        await self._on_reply(reply)


def _disable_view(view: ui.View) -> None:
    for child in view.children:
        child.disabled = True


async def _send_question(
    channel: discord.abc.Messageable,
    client: OpencodeClient,
    request: dict,
) -> None:
    """Render one question request and wire its callbacks to the REST client."""
    rid = request.get("id", "")

    async def on_submit(answers: list[list[str]]) -> None:
        try:
            await client.reply_question(rid, answers)
        except OpencodeError as e:
            _log.warning("reply_question failed for %s: %r", rid, e)

    async def on_reject() -> None:
        try:
            await client.reject_question(rid)
        except OpencodeError as e:
            _log.warning("reject_question failed for %s: %r", rid, e)

    blocks = [_question_block(q) for q in (request.get("questions") or [])]
    content = (
        f"**Question** `{rid[:12]}`\n" + "\n\n".join(blocks)
        if blocks
        else f"**Question** `{rid[:12]}` (no questions in request)"
    )
    view = QuestionView(request, on_submit, on_reject)
    try:
        msg = await channel.send(content, view=view)
    except discord.HTTPException as e:
        _log.warning("failed to send question message for %s: %r", rid, e)
        return

    async def _finalize() -> None:
        _disable_view(view)
        try:
            await msg.edit(content=msg.content + "\n_Answered._", view=view)
        except discord.HTTPException:
            pass

    async def _fail(text: str) -> None:
        _disable_view(view)
        try:
            await msg.edit(content=msg.content + f"\n_Failed: {text}_", view=view)
        except discord.HTTPException:
            pass

    # Wrap the callbacks so the message reflects the outcome.
    async def on_submit_wrapped(answers: list[list[str]]) -> None:
        try:
            await client.reply_question(rid, answers)
            await _finalize()
        except OpencodeError as e:
            await _fail(str(e))

    async def on_reject_wrapped() -> None:
        try:
            await client.reject_question(rid)
            await _finalize()
        except OpencodeError as e:
            await _fail(str(e))

    # Re-point the view's callbacks at the wrapped versions.
    view._on_submit = on_submit_wrapped  # type: ignore[assignment]
    view._on_reject = on_reject_wrapped  # type: ignore[assignment]


async def _send_permission(
    channel: discord.abc.Messageable,
    client: OpencodeClient,
    request: dict,
) -> None:
    """Render one permission request and wire its callback to the REST client."""
    rid = request.get("id", "")

    async def on_reply(reply: str) -> None:
        try:
            await client.reply_permission(rid, reply)
        except OpencodeError as e:
            _log.warning("reply_permission failed for %s: %r", rid, e)

    content = f"**Permission request** `{rid[:12]}`\n" + _permission_block(request)
    view = PermissionView(request, on_reply)
    try:
        msg = await channel.send(content, view=view)
    except discord.HTTPException as e:
        _log.warning("failed to send permission message for %s: %r", rid, e)
        return

    async def on_reply_wrapped(reply: str) -> None:
        try:
            await client.reply_permission(rid, reply)
            _disable_view(view)
            await msg.edit(content=msg.content + "\n_Resolved._", view=view)
        except OpencodeError as e:
            _disable_view(view)
            await msg.edit(content=msg.content + f"\n_Failed: {e}_", view=view)

    view._on_reply = on_reply_wrapped  # type: ignore[assignment]


async def poll_pending_requests(
    client: OpencodeClient,
    session_id: str,
    channel: discord.abc.Messageable,
    *,
    interval: float = _POLL_INTERVAL,
    stop_event: asyncio.Event,
    voice_session: VoiceSession | None = None,
) -> None:
    """Poll ``GET /question`` + ``GET /permission`` and surface matching requests.

    Runs concurrently with ``poll_until_idle`` inside ``_drive_session``. Each
    iteration fetches both lists, filters to entries whose ``sessionID`` matches
    ``session_id``, and renders any request not already in ``seen`` as a
    Discord message with buttons/selects. The loop exits when ``stop_event`` is
    set (session went idle or timed out) or when cancelled (``/oc_abort``,
    bot shutdown).

    When ``voice_session`` is set (a `/oc_voice` session is active), question
    requests are routed through the voice channel instead of buttons: the
    question text is spoken via TTS, the user's spoken answer is captured and
    transcribed, and the answer is posted back via the REST API. If the voice
    answer is empty (transcription failed or silence), it falls back to the
    button UI so the session isn't blocked. Permission requests always use the
    button UI (voice can't grant permissions meaningfully).

    Surfaced-but-unanswered requests get a best-effort ``reject`` on exit so the
    agent's ``question`` tool returns "dismissed" instead of parking until the
    opencode server restarts. Permission requests left open are left for the
    server's session finalizer (rejecting a permission mid-flight is risky; the
    server tears it down on session close anyway).
    """
    seen: set[str] = set()
    surfaced_questions: set[str] = set()
    try:
        while not stop_event.is_set():
            try:
                questions = await client.list_questions()
            except OpencodeError as e:
                _log.warning("list_questions failed: %r", e)
                questions = []
            try:
                permissions = await client.list_permissions()
            except OpencodeError as e:
                _log.warning("list_permissions failed: %r", e)
                permissions = []

            for req in questions:
                if not isinstance(req, dict):
                    continue
                if req.get("sessionID") != session_id:
                    continue
                rid = req.get("id", "")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                surfaced_questions.add(rid)
                # Voice path: speak the question, capture + transcribe the
                # spoken answer, post it back. Falls back to buttons on empty.
                if voice_session is not None:
                    answered = await _voice_answer_question(
                        voice_session, client, req, channel
                    )
                    if answered:
                        continue
                await _send_question(channel, client, req)

            for req in permissions:
                if not isinstance(req, dict):
                    continue
                if req.get("sessionID") != session_id:
                    continue
                rid = req.get("id", "")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                await _send_permission(channel, client, req)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    finally:
        # Best-effort cleanup: reject surfaced questions that were never
        # answered so the agent turn doesn't hang on a parked deferred.
        for rid in list(surfaced_questions):
            try:
                await client.reject_question(rid)
            except (OpencodeError, Exception) as e:  # noqa: BLE001
                _log.debug("cleanup reject for %s: %r", rid, e)


async def _voice_answer_question(
    voice_session: VoiceSession,
    client: OpencodeClient,
    request: dict,
    channel: discord.abc.Messageable,
) -> bool:
    """Speak a question via TTS, capture the spoken answer, post it back.

    Returns True if the answer was captured and posted successfully, False if
    the voice path failed (so the caller falls back to the button UI). Builds
    the question text from the request's `questions` list using
    `_question_block`, speaks it, records a short reply, transcribes, and
    calls `reply_question`. Best-effort — any error returns False.
    """
    rid = request.get("id", "")
    try:
        blocks = [_question_block(q) for q in (request.get("questions") or [])]
        text = "\n".join(blocks) if blocks else "(no questions in request)"
        await channel.send(f"**Question** `{rid[:12]}` (asking via voice…)\n{text}")
        answer = await voice_session.handle_question(text)
        if not answer:
            await channel.send("_(no spoken answer detected; falling back to buttons)_")
            return False
        await client.reply_question(rid, [[answer]])
        await channel.send(f"_Answered via voice:_{answer}")
        return True
    except Exception as e:  # noqa: BLE001 — voice path is best-effort
        _log.warning("voice answer for %s failed: %r", rid, e)
        return False
