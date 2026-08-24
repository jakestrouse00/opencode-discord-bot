"""REST-based equivalent of ``opencode_discord_bot.questions.poll_pending_requests``
for the Comulytic bridge.

The main Discord bot (``commands.py``) surfaces opencode's blocking
``question`` / ``permission`` requests as Discord buttons / select menus via
``questions.py:poll_pending_requests``. That module imports Pycord at module
top (``import discord`` + ``from discord import ui``) and relies on a live
gateway connection to receive button-click interactions. The Comulytic
bridge deliberately avoids importing Pycord on its happy path and does NOT
connect to the gateway (a second gateway connection with the same bot token
would kick the main bot's connection into a reconnect loop).

This module provides the same functional surface — surfacing pending
``question`` / ``permission`` requests for a given opencode session and
resolving them via the opencode REST API — but using plain-text messages
posted via ``DiscordRest`` (raw httpx REST) and polling
``GET /channels/{id}/messages`` for the user's plain-text reply.

Wire format (same as ``questions.py`` — copied from
``packages/schema/src/v1/question.ts`` + ``.../v1/permission.ts``):

  Question.Request = { id, sessionID, questions: Question.Info[], tool? }
  Question.Info    = { question, header, options: Option[], multiple?, custom? }
  Question.Option  = { label, description }
  Question answer  = string[][]  (one array of selected labels per question)

  Permission.Request = { id, sessionID, permission, patterns, metadata, always, tool? }
  Permission reply   = { reply: "once"|"always"|"reject", message? }

Reply parsing (the user types plain text in the channel):
  - Single question with options: a number ("1") picks that 1-based option;
    any other non-empty text is treated as a custom answer (only when
    ``info.custom`` is not False). Multi-select is not supported via plain
    text — the user types one label or number and we send a single-element
    list (a simplification; the multi-question path below handles N>1).
  - Multiple questions: the poller posts each question as its own message
    ("Question i of N"), waits for one reply per message, then posts the
    next. Each reply is parsed as above.
  - Permission: "y"/"yes" -> "once"; "always" -> "always"; "n"/"no" ->
    "reject".

Never imports Pycord — safe to import from the bridge's happy path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from opencode_discord_bot.opencode_client import OpencodeClient, OpencodeError
from opencode_discord_bot.text_utils import _split_message

if TYPE_CHECKING:
    from opencode_discord_bot.discord_rest import DiscordRest, DiscordRestError

_log = logging.getLogger("comulytic.bridge_questions")

# How long to wait between polling GET /question + GET /permission. Matches
# the main bot's cadence (questions.py:_POLL_INTERVAL = 2.0).
_DEFAULT_POLL_INTERVAL = 2.0

# How long to wait for the user's plain-text reply in a channel before
# rejecting the question. The main bot uses no timeout (buttons wait
# indefinitely until the session ends); plain-text polling needs a ceiling.
_DEFAULT_QUESTION_TIMEOUT = 300.0

# How often to poll GET /channels/{id}/messages for the user's reply.
_REPLY_POLL_INTERVAL = 2.0


def _fmt_options(options: list[dict], limit: int = 10) -> str:
    """Render an option list as a numbered text block.

    Copy of ``questions.py:_fmt_options`` (pure rendering, no Discord imports)
    so this module stays Pycord-free.
    """
    lines = []
    for i, opt in enumerate(options[:limit], 1):
        label = opt.get("label", "?")
        desc = opt.get("description", "")
        lines.append(f"**{i}.** {label}" + (f" — {desc}" if desc else ""))
    if len(options) > limit:
        lines.append(f"_(+{len(options) - limit} more)_")
    return "\n".join(lines)


def _question_block(info: dict) -> str:
    """Render one Question.Info as a header + options text block.

    Copy of ``questions.py:_question_block`` (pure rendering).
    """
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
        tags.append("multi-select (type one — first match wins)")
    if custom:
        tags.append("custom allowed (type any text)")
    if tags:
        out.append(f"_({', '.join(tags)})_")
    return "\n".join(out)


def _permission_block(request: dict) -> str:
    """Render a permission request as a header + patterns + metadata block.

    Copy of ``questions.py:_permission_block`` (pure rendering).
    """
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
    out.append("Reply with `y` (once), `always`, or `n` (reject).")
    return "\n".join(out)


def _parse_question_reply(text: str, info: dict) -> list[str] | None:
    """Parse one user reply into a list of selected labels for one question.

    Returns ``[label]`` (single-element list, since plain-text can't multi-select)
    or ``None`` if the reply is empty/unparseable.

    Rules:
      - Empty/whitespace -> None.
      - A number -> the option at that 1-based index (if in range). If the
        number is out of range, fall through to custom.
      - Any other non-empty text -> if it matches an option label (case-
        insensitive exact), use that label. Else if ``info.custom`` is not
        False, treat it as a custom answer (use the typed text verbatim).
        Else None (custom not allowed and no label matched).
    """
    reply = text.strip()
    if not reply:
        return None
    options = info.get("options") or []
    # Numeric -> option index.
    if reply.isdigit():
        idx = int(reply) - 1
        if 0 <= idx < len(options):
            label = options[idx].get("label")
            if label:
                return [label]
        # Out of range: fall through to custom (the typed number may be the
        # answer, e.g. a numeric answer to a non-option question).
    # Exact label match (case-insensitive).
    for opt in options:
        label = opt.get("label", "")
        if label and label.lower() == reply.lower():
            return [label]
    # Custom answer.
    if info.get("custom") is not False:
        return [reply]
    return None


def _parse_permission_reply(text: str) -> str | None:
    """Parse a y/yes/always/n/no reply into "once"|"always"|"reject" or None."""
    reply = text.strip().lower()
    if not reply:
        return None
    if reply in ("y", "yes", "ok", "approve", "once"):
        return "once"
    if reply == "always":
        return "always"
    if reply in ("n", "no", "reject", "deny"):
        return "reject"
    return None


async def _post(rest: "DiscordRest", channel_id: int, content: str) -> int | None:
    """Post a message (chunked via _split_message) and return the first message id.

    Discord caps content at 2000 chars; long question blocks are split. The
    first chunk's id is returned so the reply poller can use it as the
    ``after`` snowflake (poll for messages AFTER the question was posted).
    """
    chunks = _split_message(content)
    first_id: int | None = None
    for chunk in chunks:
        try:
            msg = await rest.create_message(channel_id, chunk)
        except Exception as exc:  # noqa: BLE001 — DiscordRestError or network
            _log.warning("post to channel %s failed: %s", channel_id, exc)
            return first_id
        if first_id is None:
            mid = msg.get("id") if isinstance(msg, dict) else None
            if mid is not None:
                try:
                    first_id = int(mid)
                except (TypeError, ValueError):
                    first_id = None
    return first_id


async def _await_reply(
    rest: "DiscordRest",
    channel_id: int,
    after_msg_id: int,
    bot_user_id: int | None,
    timeout: float,
) -> str | None:
    """Poll GET /channels/{id}/messages for the first non-bot message after
    ``after_msg_id``. Returns its content, or None on timeout.

    ``bot_user_id`` (if known) filters the bot's own posted messages out so
    we don't pick up our own question/progress messages as the "reply". If
    unknown (None), we skip messages whose author.bot is True instead.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    seen_total = 0  # debug counter for the timeout diagnostic
    while asyncio.get_event_loop().time() < deadline:
        try:
            msgs = await rest.list_messages(channel_id, after=after_msg_id, limit=50)
        except Exception as exc:  # noqa: BLE001
            _log.warning("list_messages on %s failed: %s", channel_id, exc)
            await asyncio.sleep(_REPLY_POLL_INTERVAL)
            continue
        seen_total += len(msgs)
        # list_messages returns newest-first; iterate oldest-first so the
        # earliest non-bot reply wins.
        for m in reversed(msgs):
            if not isinstance(m, dict):
                continue
            author = m.get("author") or {}
            if bot_user_id is not None and author.get("id") == str(bot_user_id):
                continue
            if author.get("bot"):
                continue
            content = m.get("content") or ""
            if content.strip():
                return content
        await asyncio.sleep(_REPLY_POLL_INTERVAL)
    # Diagnostic: if we timed out without finding a reply, log how many
    # messages we saw total across polls and which filter (bot_user_id vs
    # author.bot) was active. A persistently low seen_total with no reply
    # suggests the Message Content privileged intent is off (Discord
    # returns empty `content` for user messages without it); a high
    # seen_total with no reply suggests all messages were filtered as
    # bot-authored (bot_user_id mismatch or a misidentified bot flag).
    _log.warning(
        "_await_reply timed out on channel %s after %.0fs (after_msg=%s, "
        "bot_user_id=%s, saw %d messages total, none matched as a user reply)",
        channel_id,
        timeout,
        after_msg_id,
        bot_user_id,
        seen_total,
    )
    return None


async def _handle_question(
    client: OpencodeClient,
    rest: "DiscordRest",
    channel_id: int,
    request: dict,
    *,
    bot_user_id: int | None,
    question_timeout: float,
) -> bool:
    """Surface one question request and wait for the user's plain-text reply.

    Posts each question one at a time (labelled "Question i of N"), waits
    for the user's reply to each before posting the next, parses each
    into a label list, and calls ``reply_question`` with the assembled
    answers. On timeout or parse failure for any single question, calls
    ``reject_question``. Returns True if the question was resolved
    (replied or rejected), False on a post failure.
    """
    rid = request.get("id", "")
    questions = request.get("questions") or []
    if not questions:
        # Nothing to ask — acknowledge so the agent doesn't park.
        try:
            await client.reply_question(rid, [])
        except OpencodeError as exc:
            _log.warning("empty reply_question for %s failed: %s", rid, exc)
        return True

    answers: list[list[str]] = []
    after_id: int | None = None
    last_question_msg_id: int | None = None
    last_single_content: str | None = None
    n = len(questions)
    for idx, q in enumerate(questions):
        single_content = (
            f"**Question {idx + 1} of {n}** `{rid[:12]}`\n"
            + _question_block(q)
            + f"\n\n_Reply in this channel. You have {int(question_timeout)}s._"
        )
        question_msg_id = await _post(rest, channel_id, single_content)
        if question_msg_id is None:
            _log.warning(
                "could not post question %s (idx %d) — rejecting to unblock the agent",
                rid,
                idx,
            )
            try:
                await client.reject_question(rid)
            except OpencodeError as exc:
                _log.warning("reject_question fallback for %s failed: %s", rid, exc)
            return False
        last_question_msg_id = question_msg_id
        last_single_content = single_content
        # For the first question, poll for replies after the question message.
        # For subsequent questions, poll for replies after the previous reply
        # (after_id was advanced past the consumed reply at the end of the
        # previous iteration).
        poll_after = after_id if after_id is not None else question_msg_id
        reply = await _await_reply(
            rest, channel_id, poll_after, bot_user_id, question_timeout
        )
        if reply is None:
            _log.warning(
                "question %s (idx %d) timed out after %.0fs — rejecting",
                rid,
                idx,
                question_timeout,
            )
            try:
                await client.reject_question(rid)
            except OpencodeError as exc:
                _log.warning("reject_question for %s failed: %s", rid, exc)
            try:
                await rest.edit_message(
                    channel_id,
                    question_msg_id,
                    single_content
                    + f"\n_Timed out (question {idx + 1} unanswered) — rejected._",
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        parsed = _parse_question_reply(reply, q)
        if parsed is None:
            _log.warning(
                "question %s (idx %d): could not parse reply %r — rejecting",
                rid,
                idx,
                reply,
            )
            try:
                await client.reject_question(rid)
            except OpencodeError as exc:
                _log.warning("reject_question for %s failed: %s", rid, exc)
            try:
                await rest.edit_message(
                    channel_id,
                    question_msg_id,
                    single_content
                    + f"\n_Could not parse reply for question {idx + 1} — rejected._",
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        answers.append(parsed)
        # Advance the after-snowflake past this reply so the next iteration's
        # _await_reply poll doesn't re-pick this consumed reply.
        after_id = await _find_reply_msg_id(
            rest, channel_id, poll_after, bot_user_id, reply
        )

    summary_content = "_All questions answered — submitting._"
    summary_msg_id = await _post(rest, channel_id, summary_content)
    submit_edit_id = summary_msg_id if summary_msg_id is not None else last_question_msg_id
    submit_edit_base = (
        summary_content if summary_msg_id is not None else (last_single_content or "")
    )

    try:
        await client.reply_question(rid, answers)
    except OpencodeError as exc:
        _log.warning("reply_question for %s failed: %s", rid, exc)
        try:
            await rest.edit_message(
                channel_id,
                submit_edit_id,
                submit_edit_base + f"\n_Failed to submit answer: {exc}_",
            )
        except Exception:  # noqa: BLE001
            pass
        return True
    try:
        await rest.edit_message(
            channel_id,
            submit_edit_id,
            submit_edit_base + "\n_Answered._",
        )
    except Exception:  # noqa: BLE001
        pass
    return True


async def _find_reply_msg_id(
    rest: "DiscordRest",
    channel_id: int,
    after_msg_id: int,
    bot_user_id: int | None,
    reply_content: str,
) -> int:
    """Find the message id of the first non-bot message with content matching
    ``reply_content`` (case-insensitive, stripped) after ``after_msg_id``.

    Used to advance the ``after`` cursor between consecutive questions in a
    multi-question request so the next _await_reply poll doesn't re-pick
    the same message. Returns the original ``after_msg_id`` on any failure
    (degrades to potentially re-finding the same reply, which is safe — the
    parser already consumed it, and a re-find would just be a no-op poll).
    """
    try:
        msgs = await rest.list_messages(channel_id, after=after_msg_id, limit=50)
    except Exception:  # noqa: BLE001
        return after_msg_id
    target = reply_content.strip().lower()
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        author = m.get("author") or {}
        if bot_user_id is not None and author.get("id") == str(bot_user_id):
            continue
        if author.get("bot"):
            continue
        content = (m.get("content") or "").strip().lower()
        if content == target:
            mid = m.get("id")
            if mid is not None:
                try:
                    return int(mid)
                except (TypeError, ValueError):
                    pass
    return after_msg_id


async def _handle_permission(
    client: OpencodeClient,
    rest: "DiscordRest",
    channel_id: int,
    request: dict,
    *,
    bot_user_id: int | None,
    question_timeout: float,
) -> bool:
    """Surface one permission request and wait for the user's y/n reply."""
    rid = request.get("id", "")
    content = f"**Permission request** `{rid[:12]}`\n" + _permission_block(request)
    msg_id = await _post(rest, channel_id, content)
    if msg_id is None:
        _log.warning("could not post permission %s — leaving for server finalizer", rid)
        return False
    reply = await _await_reply(rest, channel_id, msg_id, bot_user_id, question_timeout)
    if reply is None:
        _log.warning("permission %s timed out — leaving for server finalizer", rid)
        try:
            await rest.edit_message(
                channel_id, msg_id, content + "\n_Timed out — not resolved._"
            )
        except Exception:  # noqa: BLE001
            pass
        return True
    parsed = _parse_permission_reply(reply)
    if parsed is None:
        _log.warning("permission %s: could not parse reply %r", rid, reply)
        try:
            await rest.edit_message(
                channel_id, msg_id, content + f"\n_Could not parse reply: {reply!r}_"
            )
        except Exception:  # noqa: BLE001
            pass
        return True
    try:
        await client.reply_permission(rid, parsed)
    except OpencodeError as exc:
        _log.warning("reply_permission for %s failed: %s", rid, exc)
        try:
            await rest.edit_message(channel_id, msg_id, content + f"\n_Failed: {exc}_")
        except Exception:  # noqa: BLE001
            pass
        return True
    try:
        await rest.edit_message(channel_id, msg_id, content + "\n_Resolved._")
    except Exception:  # noqa: BLE001
        pass
    return True


async def poll_pending_requests_rest(
    client: OpencodeClient,
    session_id: str,
    rest: "DiscordRest",
    channel_id: int,
    *,
    stop_event: asyncio.Event,
    interval: float = _DEFAULT_POLL_INTERVAL,
    question_timeout: float = _DEFAULT_QUESTION_TIMEOUT,
    bot_user_id: int | None = None,
) -> None:
    """Poll GET /question + GET /permission and surface matching requests.

    REST-based equivalent of ``questions.py:poll_pending_requests``. Runs
    concurrently with ``poll_until_idle`` inside the bridge's
    ``route_to_assistant``. Each iteration fetches both lists, filters to
    entries whose ``sessionID`` matches ``session_id``, and renders any
    request not already in ``seen`` as a plain-text message in
    ``channel_id`` (via ``rest.create_message``). Then waits for the user's
    plain-text reply (via ``_await_reply`` polling GET /channels/{id}/messages)
    and posts the choice back to the opencode REST API so the deferred
    resolves and the agent turn resumes.

    Surfaced-but-unanswered questions get a best-effort ``reject`` on exit so
    the agent's ``question`` tool returns "dismissed" instead of parking
    forever. Permission requests left open are left for the server's session
    finalizer (rejecting a permission mid-flight is risky).

    ``bot_user_id`` (the bot's own Discord user id) is used to filter the
    bot's own posted messages out of the reply-poll. If None (unknown),
    we fall back to filtering ``author.bot == True``.
    """
    seen: set[str] = set()
    surfaced_questions: set[str] = set()
    cur_interval = interval
    try:
        while not stop_event.is_set():
            try:
                questions_result, permissions_result = await asyncio.gather(
                    client.list_questions(),
                    client.list_permissions(),
                    return_exceptions=True,
                )
            except Exception as exc:  # noqa: BLE001 — gather shouldn't raise
                _log.warning(
                    "gather of list_questions/list_permissions failed: %s", exc
                )
                questions_result = []
                permissions_result = []
            questions = (
                questions_result
                if isinstance(questions_result, list)
                else ([_log.warning("list_questions: %r", questions_result)] or [])
            )
            permissions = (
                permissions_result
                if isinstance(permissions_result, list)
                else ([_log.warning("list_permissions: %r", permissions_result)] or [])
            )

            new_requests = 0
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
                new_requests += 1
                await _handle_question(
                    client,
                    rest,
                    channel_id,
                    req,
                    bot_user_id=bot_user_id,
                    question_timeout=question_timeout,
                )

            for req in permissions:
                if not isinstance(req, dict):
                    continue
                if req.get("sessionID") != session_id:
                    continue
                rid = req.get("id", "")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                new_requests += 1
                await _handle_permission(
                    client,
                    rest,
                    channel_id,
                    req,
                    bot_user_id=bot_user_id,
                    question_timeout=question_timeout,
                )

            # Adaptive backoff (mirrors questions.py:652-659).
            if new_requests > 0:
                cur_interval = interval
            else:
                cur_interval = min(cur_interval * 1.5, interval * 2.0)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=cur_interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    finally:
        # Best-effort cleanup: reject surfaced questions that were never
        # answered so the agent turn doesn't hang on a parked deferred.
        # Permissions are left for the server's session finalizer.
        if surfaced_questions:
            results = await asyncio.gather(
                *(client.reject_question(rid) for rid in list(surfaced_questions)),
                return_exceptions=True,
            )
            for rid, res in zip(surfaced_questions, results):
                if isinstance(res, Exception):  # noqa: BLE001
                    _log.debug("cleanup reject for %s: %r", rid, res)
