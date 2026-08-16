"""Unit tests for ``questions.py`` — pure helpers + ``poll_pending_requests``.

``poll_pending_requests`` is exercised via a ``FakeChannel`` that records
``send`` calls and captures the ``QuestionView`` / ``PermissionView`` so
the test can simulate a button click by invoking the view's callback.

The poll loop is stopped by side-effect callbacks registered via
``client.on_call`` so the test doesn't deadlock waiting for a button click
that happens after the loop exits.
"""

import asyncio

import pytest

from opencode_discord_bot import questions as questions_mod
from tests.fakes import (
    ScriptedOpencodeClient,
    FakeChannel,
    FakeInteraction,
    question_request,
    permission_request,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_fmt_options_caps_at_limit():
    options = [{"label": f"opt{i}", "description": ""} for i in range(15)]
    out = questions_mod._fmt_options(options, limit=10)
    assert "+5 more" in out


def test_fmt_options_no_description_omitted():
    out = questions_mod._fmt_options([{"label": "x"}])
    assert "**1.** x" in out


def test_fmt_options_with_description():
    out = questions_mod._fmt_options([{"label": "x", "description": "desc"}])
    assert "x — desc" in out


def test_question_block_includes_header():
    block = questions_mod._question_block({"header": "Pick one", "question": "which?",
                                            "options": [{"label": "a"}]})
    assert "Pick one" in block
    assert "which?" in block


def test_question_block_tags_multi_and_custom():
    block = questions_mod._question_block({"header": "h", "multiple": True, "custom": True})
    assert "multi-select" in block
    assert "custom allowed" in block


def test_permission_block_includes_patterns():
    block = questions_mod._permission_block({
        "permission": "edit", "patterns": ["src/x.py"], "metadata": {"k": "v"}
    })
    assert "Permission: edit" in block
    assert "src/x.py" in block
    assert "k" in block


def test_permission_block_doom_loop_adds_warning():
    block = questions_mod._permission_block({"permission": "doom_loop", "patterns": []})
    assert "stuck in a loop" in block


# ---------------------------------------------------------------------------
# poll_pending_requests (button path)
# ---------------------------------------------------------------------------


async def test_no_requests_exits_clean():
    client = ScriptedOpencodeClient()
    channel = FakeChannel()
    stop_event = asyncio.Event()
    stop_event.set()  # already done — loop should not even enter
    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    assert channel.sent == []  # nothing posted


async def test_one_question_surfaces_view():
    """One question is surfaced as a Discord message with a QuestionView."""
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [],
                  [])
    client.script("list_permissions", [], [], [])
    channel = FakeChannel()
    stop_event = asyncio.Event()

    # Stop the loop after the first list_questions call returns the question.
    def _stop_after_first_poll(queue):
        if queue is not None and len(queue) == 2:  # the first (with question) was popped
            asyncio.get_event_loop().call_later(0.01, stop_event.set)
    client.on_call("list_questions", _stop_after_first_poll)

    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    # A question message was posted with a QuestionView.
    assert len(channel.sent) >= 1
    _, kw = channel.sent[0]
    assert kw["view"] is not None


async def test_question_button_click_calls_reply_question():
    """Simulate a button click on a surfaced QuestionView → reply_question."""
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [],
                  [])
    client.script("list_permissions", [], [])
    client.script("reply_question", True)
    channel = FakeChannel()
    stop_event = asyncio.Event()

    def _stop_after_first(queue):
        if queue is not None and len(queue) == 2:
            asyncio.get_event_loop().call_later(0.01, stop_event.set)
    client.on_call("list_questions", _stop_after_first)

    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    _, kw = channel.sent[0]
    view = kw["view"]
    # Find the first option button (label "main.py" per question_request).
    option_btn = next(
        c for c in view.children
        if hasattr(c, "label") and c.label in ("main.py", "opt 0")
    )
    await option_btn.callback(FakeInteraction())
    # reply_question was called.
    assert any(c[0] == "reply_question" for c in client.calls)


async def test_permission_button_click_calls_reply_permission():
    """Simulate clicking Allow once on a surfaced PermissionView."""
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
    _, kw = channel.sent[0]
    view = kw["view"]
    labels = [c.label for c in view.children if hasattr(c, "label")]
    assert "Allow once" in labels
    allow_btn = next(c for c in view.children if getattr(c, "label", None) == "Allow once")
    await allow_btn.callback(FakeInteraction())
    assert any(c[0] == "reply_permission" for c in client.calls)


async def test_unanswered_question_rejected_on_exit():
    """A surfaced-but-unanswered question is rejected in the finally block."""
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-1", sid="sid-1")],
                  [])
    client.script("list_permissions", [], [])
    client.script("reject_question", True)
    channel = FakeChannel()
    stop_event = asyncio.Event()

    def _stop_after_first(queue):
        if queue is not None and len(queue) == 1:  # 1 remaining after pop
            asyncio.get_event_loop().call_later(0.01, stop_event.set)
    client.on_call("list_questions", _stop_after_first)

    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    # The finally block should reject the surfaced question.
    assert any(c[0] == "reject_question" and c[1][0] == "q-1" for c in client.calls)


async def test_filters_other_session_ids():
    """A question for a different sessionID is not surfaced."""
    client = ScriptedOpencodeClient()
    client.script("list_questions",
                  [question_request(rid="q-other", sid="other-sid")],
                  [])
    client.script("list_permissions", [], [])
    channel = FakeChannel()
    stop_event = asyncio.Event()
    stop_event.set()
    await questions_mod.poll_pending_requests(
        client, "sid-1", channel, interval=0.0, stop_event=stop_event
    )
    assert channel.sent == []