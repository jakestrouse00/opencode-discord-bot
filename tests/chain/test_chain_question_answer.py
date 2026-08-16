"""Focused chain: question asked + answered, and permission asked + approved, on both paths.

Button path (``questions.poll_pending_requests``) and REST path
(``bridge_questions.poll_pending_requests_rest``) are each exercised with
a scripted opencode + (for REST) scripted DiscordRest. The button path
simulates a click by invoking the view's button callback; the REST path
scripts ``list_messages`` to return a user reply.

The poll loops are stopped via ``on_call`` side-effects so the test doesn't
deadlock waiting for a button click that happens after the loop exits.
"""

import asyncio

import pytest

from opencode_discord_bot import questions as questions_mod
from opencode_discord_bot import bridge_questions as bq
from tests.fakes import (
    ScriptedOpencodeClient,
    ScriptedDiscordRest,
    FakeChannel,
    FakeInteraction,
    question_request,
    permission_request,
    discord_message_dict,
)


# ---------------------------------------------------------------------------
# Button path (questions.poll_pending_requests)
# ---------------------------------------------------------------------------


async def test_chain_question_button_path_asked_and_answered():
    """One question surfaced via buttons -> user clicks an option -> reply_question."""
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [],
                  [])
    client.script("list_permissions", [], [], [])
    client.script("reply_question", True)
    channel = FakeChannel()
    stop_event = asyncio.Event()

    # Stop the loop after the first poll surfaces the question (the queue
    # shrinks from 3 to 2 entries after the pop).
    def _stop_after_first(queue):
        if queue is not None and len(queue) == 2:
            asyncio.get_event_loop().call_later(0.01, stop_event.set)
    client.on_call("list_questions", _stop_after_first)

    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    # A question message with a QuestionView was posted.
    assert channel.sent
    _, kw = channel.sent[0]
    view = kw["view"]
    assert view is not None
    # Click the first option button.
    option_btn = next(
        c for c in view.children
        if hasattr(c, "label") and c.label in ("main.py", "opt 0")
    )
    await option_btn.callback(FakeInteraction())
    # reply_question was called with the option label.
    reply_calls = [c for c in client.calls if c[0] == "reply_question"]
    assert reply_calls
    assert reply_calls[0][1][1] == [["main.py"]]


async def test_chain_permission_button_path_asked_and_approved():
    """One permission surfaced via buttons -> user clicks Allow once -> reply_permission."""
    client = ScriptedOpencodeClient()
    client.script("list_questions", [], [])
    client.script("list_permissions",
                  [permission_request(rid="p-1", sid="sid-1")],
                  [],
                  [])
    client.script("reply_permission", True)
    channel = FakeChannel()
    stop_event = asyncio.Event()

    def _stop_after_first(queue):
        if queue is not None and len(queue) == 2:
            asyncio.get_event_loop().call_later(0.01, stop_event.set)
    client.on_call("list_permissions", _stop_after_first)

    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    assert channel.sent
    _, kw = channel.sent[0]
    view = kw["view"]
    allow_btn = next(c for c in view.children if getattr(c, "label", None) == "Allow once")
    await allow_btn.callback(FakeInteraction())
    perm_calls = [c for c in client.calls if c[0] == "reply_permission"]
    assert perm_calls
    assert perm_calls[0][1][1] == "once"


# ---------------------------------------------------------------------------
# REST path (bridge_questions.poll_pending_requests_rest)
# ---------------------------------------------------------------------------


async def test_chain_question_rest_path_asked_and_answered():
    """One question via REST -> user types a number reply -> reply_question."""
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [],
                  [])
    client.script("list_permissions", [], [], [])
    client.script("reply_question", True)
    rest = ScriptedDiscordRest()
    # list_messages returns the user reply "1" once, then empty.
    rest.script("list_messages",
                [discord_message_dict(mid=2000, content="1", author_id="9999")],
                [])
    stop_event = asyncio.Event()

    # Stop the loop after the question is surfaced (queue shrinks to 2).
    def _stop_after_first(queue):
        if queue is not None and len(queue) == 2:
            asyncio.get_event_loop().call_later(0.1, stop_event.set)
    client.on_call("list_questions", _stop_after_first)

    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=42,
        stop_event=stop_event, interval=0.0, question_timeout=2.0, bot_user_id=100,
    )
    # The question was posted to channel 42.
    assert any(cid == 42 for cid, _ in rest.posted)
    # reply_question was called with the parsed answer.
    reply_calls = [c for c in client.calls if c[0] == "reply_question"]
    assert reply_calls
    assert reply_calls[0][1][1] == [["main.py"]]  # "1" → first option


async def test_chain_permission_rest_path_asked_and_approved():
    """One permission via REST -> user types "y" -> reply_permission "once"."""
    client = ScriptedOpencodeClient()
    client.script("list_questions", [], [])
    client.script("list_permissions",
                  [permission_request(rid="p-1", sid="sid-1")],
                  [],
                  [])
    client.script("reply_permission", True)
    rest = ScriptedDiscordRest()
    rest.script("list_messages",
                [discord_message_dict(mid=2000, content="y", author_id="9999")],
                [])
    stop_event = asyncio.Event()

    def _stop_after_first(queue):
        if queue is not None and len(queue) == 2:
            asyncio.get_event_loop().call_later(0.1, stop_event.set)
    client.on_call("list_permissions", _stop_after_first)

    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=42,
        stop_event=stop_event, interval=0.0, question_timeout=2.0, bot_user_id=100,
    )
    perm_calls = [c for c in client.calls if c[0] == "reply_permission"]
    assert perm_calls
    assert perm_calls[0][1][1] == "once"