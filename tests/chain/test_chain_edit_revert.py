"""Chain E: edit-to-revert -> abort + revert + resend + drive.

A previously-sent follow-up message is edited; the bot aborts the running
session, reverts to the mapped opencode user message, resends the edited
text, and re-drives. opencode REST is scripted.
"""

import asyncio

import pytest

from opencode_discord_bot.config import config
from opencode_discord_bot.opencode_client import OpencodeError
from tests.fakes import (
    FakeChannel,
    FakeMessage,
    FakeAuthor,
    FakeGuild,
    assistant_message,
)


@pytest.fixture
def edit_config(monkeypatch):
    monkeypatch.setattr(config, "voice_message_enabled", False)


async def test_chain_edit_revert_aborts_reverts_resends(bot_instance, edit_config, monkeypatch):
    """Edit a mapped follow-up -> abort + revert + send_prompt_async + drive.

    An active drive task is registered so the abort path is exercised (the
    source only aborts when ``sid in self._active_drives``).
    """
    sid = "sid-edit"
    ch = FakeChannel(id=555, name="edit-session", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)
    # Seed the prompt_msg_map: Discord message id 10 -> opencode message "oc-1".
    bot_instance._prompt_msg_map[555] = {10: "oc-1"}
    # Register an active drive task so the abort branch fires.
    bot_instance._active_drives[sid] = asyncio.create_task(asyncio.sleep(100))

    before = FakeMessage(
        id=10, content="original", channel=ch,
        author=FakeAuthor(id=100, name="user"), guild=ch.guild,
    )
    after = FakeMessage(
        id=10, content="edited prompt", channel=ch,
        author=FakeAuthor(id=100, name="user"), guild=ch.guild,
    )

    # Script opencode: abort, revert, send_prompt_async, busy->idle, final.
    bot_instance.client.script("abort_session", True)
    bot_instance.client.script("revert_session", {"id": sid})
    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("get_session_status",
                                {sid: {"type": "busy"}},
                                {sid: {"type": "idle"}})
    bot_instance.client.script("list_questions", [], [])
    bot_instance.client.script("list_permissions", [], [])
    user_msg = {"info": {"role": "user", "id": "u-2"}, "parts": [{"type": "text", "text": "x"}]}
    bot_instance.client.script("list_messages",
                                [user_msg],
                                [assistant_message("EDITED RESULT", mid="m-2")])

    try:
        await bot_instance.on_message_edit(before, after)
    finally:
        # The active drive task was cancelled by the edit handler; cleanup.
        task = bot_instance._active_drives.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass

    # Call order: abort_session, revert_session, send_prompt_async.
    method_order = [c[0] for c in bot_instance.client.calls]
    assert "abort_session" in method_order
    assert "revert_session" in method_order
    assert "send_prompt_async" in method_order
    ai = method_order.index("abort_session")
    ri = method_order.index("revert_session")
    si = method_order.index("send_prompt_async")
    assert ai < ri < si
    # revert was called with the mapped opencode message id.
    revert_call = next(c for c in bot_instance.client.calls if c[0] == "revert_session")
    assert revert_call[1] == (sid, "oc-1")
    # send_prompt_async got the edited text.
    prompt_call = next(c for c in bot_instance.client.calls if c[0] == "send_prompt_async")
    assert prompt_call[1][1][0]["text"] == "edited prompt"
    # The final response was posted.
    final_posts = [c for c, _ in ch.sent if c and "EDITED RESULT" in c]
    assert final_posts


async def test_chain_edit_no_mapping_is_ignored(bot_instance, edit_config):
    """An edit for a message with no mapping -> ignored (no abort/revert)."""
    sid = "sid-edit-2"
    ch = FakeChannel(id=555, name="x", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)
    # No _prompt_msg_map entry for this message id.
    before = FakeMessage(id=99, content="a", channel=ch, author=FakeAuthor(id=100))
    after = FakeMessage(id=99, content="b", channel=ch, author=FakeAuthor(id=100))
    bot_instance.client.script("abort_session", True)
    await bot_instance.on_message_edit(before, after)
    # No abort/revert/send happened.
    assert not any(c[0] == "abort_session" for c in bot_instance.client.calls)
    assert not any(c[0] == "revert_session" for c in bot_instance.client.calls)


async def test_chain_edit_unbound_channel_ignored(bot_instance, edit_config):
    """An edit in a channel with no bound session -> ignored."""
    ch = FakeChannel(id=999, name="unbound", guild=FakeGuild())
    before = FakeMessage(id=1, content="a", channel=ch, author=FakeAuthor(id=100))
    after = FakeMessage(id=1, content="b", channel=ch, author=FakeAuthor(id=100))
    bot_instance._prompt_msg_map[999] = {1: "oc-x"}
    await bot_instance.on_message_edit(before, after)
    assert not any(c[0] == "abort_session" for c in bot_instance.client.calls)


async def test_chain_edit_identical_content_ignored(bot_instance, edit_config):
    """An edit that doesn't change content -> ignored."""
    sid = "sid-same"
    ch = FakeChannel(id=555, name="x", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)
    bot_instance._prompt_msg_map[555] = {20: "oc-1"}
    before = FakeMessage(id=20, content="same", channel=ch, author=FakeAuthor(id=100))
    after = FakeMessage(id=20, content="same", channel=ch, author=FakeAuthor(id=100))
    await bot_instance.on_message_edit(before, after)
    assert not any(c[0] == "abort_session" for c in bot_instance.client.calls)


async def test_chain_edit_bot_author_ignored(bot_instance, edit_config):
    """An edit by a bot -> ignored."""
    sid = "sid-bot"
    ch = FakeChannel(id=555, name="x", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)
    bot_instance._prompt_msg_map[555] = {30: "oc-1"}
    before = FakeMessage(id=30, content="a", channel=ch, author=FakeAuthor(bot=True, id=1))
    after = FakeMessage(id=30, content="b", channel=ch, author=FakeAuthor(bot=True, id=1))
    await bot_instance.on_message_edit(before, after)
    assert not any(c[0] == "abort_session" for c in bot_instance.client.calls)