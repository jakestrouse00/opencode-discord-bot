"""Chain D: plain-text follow-up in a session channel -> opencode -> questions -> response.

Dispatches through ``bot.on_message`` so the full intake -> drive path is
exercised. The opencode client is scripted to surface one question (button
path via ``poll_pending_requests``) and then a final response.
"""

import asyncio

import pytest

from opencode_discord_bot.config import config
from opencode_discord_bot import questions as questions_mod
from tests.fakes import (
    FakeChannel,
    FakeMessage,
    FakeAuthor,
    FakeGuild,
    FakeInteraction,
    assistant_message,
    question_request,
)


@pytest.fixture
def followup_config(monkeypatch):
    """Configure for the plain-text follow-up chain."""
    monkeypatch.setattr(config, "voice_message_enabled", False)
    monkeypatch.setattr(config, "discord_bot_session_category_id", 0)


async def test_chain_text_followup_drives_session(
    bot_instance, followup_config, monkeypatch
):
    """Plain-text follow-up in a session channel -> opencode drive -> final posted."""
    sid = "sid-text"
    ch = FakeChannel(id=555, name="text-session", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)

    msg = FakeMessage(
        id=10, content="refine the plan", channel=ch,
        author=FakeAuthor(id=100, name="user"), guild=ch.guild,
    )

    # Script opencode: prompt, busy->idle, no questions, final.
    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("get_session_status",
                                {sid: {"type": "busy"}},
                                {sid: {"type": "idle"}})
    bot_instance.client.script("list_questions", [], [])
    bot_instance.client.script("list_permissions", [], [])
    user_msg = {"info": {"role": "user", "id": "u-1"}, "parts": [{"type": "text", "text": "x"}]}
    bot_instance.client.script("list_messages",
                                [user_msg],
                                [assistant_message("REFINED PLAN", mid="m-1")])

    await bot_instance.on_message(msg)

    # send_prompt_async was called with NO agent override (plain follow-up).
    prompt_calls = [c for c in bot_instance.client.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    assert prompt_calls[0][2]["agent"] is None
    # The final response was posted to the channel.
    final_posts = [c for c, _ in ch.sent if c and "REFINED PLAN" in c]
    assert final_posts, f"final not posted; sent: {[c for c,_ in ch.sent]}"


async def test_chain_text_followup_with_question_answered_via_button(
    bot_instance, followup_config, monkeypatch
):
    """Text follow-up where plan-author asks a question; user answers via button."""
    sid = "sid-q"
    ch = FakeChannel(id=555, name="q-session", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)

    msg = FakeMessage(
        id=11, content="plan something", channel=ch,
        author=FakeAuthor(id=100, name="user"), guild=ch.guild,
    )

    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("get_session_status",
                                {sid: {"type": "busy"}},
                                {sid: {"type": "idle"}})
    # First poll: one question. Second+ polls: empty.
    bot_instance.client.script("list_questions",
                                [question_request(rid="q-1", sid=sid)],
                                [],
                                [])
    bot_instance.client.script("list_permissions", [], [], [])
    bot_instance.client.script("reply_question", True)
    user_msg = {"info": {"role": "user", "id": "u-1"}, "parts": [{"type": "text", "text": "x"}]}
    bot_instance.client.script("list_messages",
                                [user_msg],
                                [assistant_message("FINAL WITH Q", mid="m-1")])

    # Stop the poller after reply_question fires.
    async def _stop_after_reply(*a, **kw):
        await asyncio.sleep(0.05)
    # The poller's stop_event is internal to _drive_session; we instead let
    # the drive finish naturally (status -> idle) and the poller will be
    # stopped by _drive_session's finally.

    await bot_instance.on_message(msg)

    # A question was surfaced (channel.send got a QuestionView).
    views = [kw.get("view") for _, kw in ch.sent if kw.get("view") is not None]
    assert views, "no view posted"
    # Simulate clicking the first option button on the QuestionView.
    view = views[0]
    option_btn = None
    for child in view.children:
        if hasattr(child, "label") and child.label in ("main.py", "opt 0"):
            option_btn = child
            break
    assert option_btn is not None
    await option_btn.callback(FakeInteraction())
    # reply_question was called.
    reply_calls = [c for c in bot_instance.client.calls if c[0] == "reply_question"]
    assert reply_calls
    # The final response was posted.
    final_posts = [c for c, _ in ch.sent if c and "FINAL WITH Q" in c]
    assert final_posts