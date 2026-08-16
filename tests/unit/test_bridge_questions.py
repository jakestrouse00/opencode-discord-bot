"""Unit tests for ``bridge_questions.py`` — pure parsers + REST poller.

``poll_pending_requests_rest`` is exercised with ``ScriptedOpencodeClient``
+ ``ScriptedDiscordRest``; the reply-poller's ``_await_reply`` reads from
``fake_rest.list_messages``, which is scripted to return a user reply after
the question is posted.
"""

import asyncio

import pytest

from opencode_discord_bot import bridge_questions as bq
from tests.fakes import (
    ScriptedOpencodeClient,
    ScriptedDiscordRest,
    question_request,
    permission_request,
    discord_message_dict,
)


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def test_parse_question_reply_empty_returns_none():
    assert bq._parse_question_reply("", {"options": []}) is None
    assert bq._parse_question_reply("   ", {"options": []}) is None


def test_parse_question_reply_numeric_picks_option():
    info = {"options": [{"label": "a"}, {"label": "b"}]}
    assert bq._parse_question_reply("1", info) == ["a"]
    assert bq._parse_question_reply("2", info) == ["b"]


def test_parse_question_reply_numeric_out_of_range_falls_to_custom():
    info = {"options": [{"label": "a"}], "custom": True}
    # "5" is out of range but custom is allowed → custom answer.
    assert bq._parse_question_reply("5", info) == ["5"]


def test_parse_question_reply_label_match_case_insensitive():
    info = {"options": [{"label": "OptionA"}], "custom": False}
    assert bq._parse_question_reply("optiona", info) == ["OptionA"]


def test_parse_question_reply_custom_when_allowed():
    info = {"options": [{"label": "a"}], "custom": True}
    assert bq._parse_question_reply("anything", info) == ["anything"]


def test_parse_question_reply_no_match_no_custom_returns_none():
    info = {"options": [{"label": "a"}], "custom": False}
    assert bq._parse_question_reply("zzz", info) is None


def test_parse_permission_reply_yes():
    assert bq._parse_permission_reply("y") == "once"
    assert bq._parse_permission_reply("yes") == "once"
    assert bq._parse_permission_reply("ok") == "once"


def test_parse_permission_reply_always():
    assert bq._parse_permission_reply("always") == "always"


def test_parse_permission_reply_no():
    assert bq._parse_permission_reply("n") == "reject"
    assert bq._parse_permission_reply("no") == "reject"
    assert bq._parse_permission_reply("reject") == "reject"


def test_parse_permission_reply_unknown_returns_none():
    assert bq._parse_permission_reply("maybe") is None
    assert bq._parse_permission_reply("") is None


# ---------------------------------------------------------------------------
# poll_pending_requests_rest
# ---------------------------------------------------------------------------


async def test_no_requests_exits_clean():
    client = ScriptedOpencodeClient()
    rest = ScriptedDiscordRest()
    stop_event = asyncio.Event()
    stop_event.set()
    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=1, stop_event=stop_event, interval=0.0
    )
    assert rest.posted == []


async def test_one_question_posted_and_user_replies():
    client = ScriptedOpencodeClient()
    # First poll: one question. Subsequent: empty.
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [],
                  [])
    client.script("list_permissions", [], [], [])
    client.script("reply_question", True)
    rest = ScriptedDiscordRest()
    # After the question is posted (msg id 1000), list_messages returns the user reply.
    # _await_reply polls; script it to return the reply on the first poll after the question.
    client.on_call("list_questions", lambda q: None)  # no-op; just record

    # The question post returns a msg id; _await_reply then polls list_messages.
    # Script list_messages to return a user reply once, then empty.
    # The first create_message (question) gets id 1000, the reply needs to come after that.
    rest.script("list_messages",
                [discord_message_dict(mid=2000, content="1", author_id="9999")],
                [])
    stop_event = asyncio.Event()

    async def _stop_after_reply(*a, **kw):
        await asyncio.sleep(0.1)
        stop_event.set()
    client.on_call("reply_question", lambda q: asyncio.create_task(_stop_after_reply()))

    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=42,
        stop_event=stop_event, interval=0.0, question_timeout=1.0,
        bot_user_id=100,
    )
    # The question was posted to channel 42.
    assert any(cid == 42 for cid, _ in rest.posted)
    # reply_question was called with the parsed answer.
    reply_calls = [c for c in client.calls if c[0] == "reply_question"]
    assert reply_calls
    assert reply_calls[0][1][1] == [["main.py"]]  # "1" → first option label


async def test_permission_posted_and_user_approves():
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

    async def _stop_after(*a, **kw):
        await asyncio.sleep(0.1)
        stop_event.set()
    client.on_call("reply_permission", lambda q: asyncio.create_task(_stop_after()))

    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=42,
        stop_event=stop_event, interval=0.0, question_timeout=1.0,
        bot_user_id=100,
    )
    assert any(cid == 42 for cid, _ in rest.posted)
    perm_calls = [c for c in client.calls if c[0] == "reply_permission"]
    assert perm_calls
    assert perm_calls[0][1][1] == "once"


async def test_filters_other_session_ids():
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-other", sid="other-sid")],
                  [])
    client.script("list_permissions", [], [])
    rest = ScriptedDiscordRest()
    stop_event = asyncio.Event()
    stop_event.set()
    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=42, stop_event=stop_event, interval=0.0
    )
    assert rest.posted == []


async def test_unanswered_question_rejected_on_exit():
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [])
    client.script("list_permissions", [], [])
    client.script("reject_question", True)
    rest = ScriptedDiscordRest()
    # list_messages always returns empty → _await_reply times out.
    rest.script("list_messages", [])
    stop_event = asyncio.Event()

    async def _stop_after(*a, **kw):
        await asyncio.sleep(0.2)
        stop_event.set()
    # Stop after the first poll surfaces the question (no reply will come).
    client.on_call("list_questions", lambda q: asyncio.create_task(_stop_after()))

    # Use a very short question_timeout so _await_reply returns fast.
    await bq.poll_pending_requests_rest(
        client, "sid-1", rest, channel_id=42,
        stop_event=stop_event, interval=0.0, question_timeout=0.05,
        bot_user_id=100,
    )
    # The finally should reject the surfaced question.
    reject_calls = [c for c in client.calls if c[0] == "reject_question"]
    assert reject_calls