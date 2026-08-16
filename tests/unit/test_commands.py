"""Unit tests for ``commands.py`` — pure helpers + dispatch + drive session.

These use the ``bot_instance`` fixture (no gateway, no serve, scripted
opencode client, real SessionRouter at tmp_path). ``generate_slug`` is
stubbed so no Ollama call happens. The four ``on_message`` branches are
exercised by mocking the dispatched-to helper methods and asserting they're
called with the right arguments.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from opencode_discord_bot import commands as commands_mod
from opencode_discord_bot.config import config
from tests.fakes import (
    FakeChannel,
    FakeMessage,
    FakeAttachment,
    FakeAuthor,
    FakeGuild,
    assistant_message,
    question_request,
)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def test_log_preview_short_text():
    assert commands_mod._log_preview("hello") == "hello"


def test_log_preview_long_text_truncates():
    result = commands_mod._log_preview("x" * 100, max_len=20)
    # max_len-1 chars + "…" = max_len total.
    assert len(result) == 20
    assert result.endswith("…")


def test_log_preview_collapses_whitespace():
    assert commands_mod._log_preview("hello   world") == "hello world"


def test_channel_allowed_empty_list_allows_all(monkeypatch):
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [])
    assert commands_mod._channel_allowed(123) is True


def test_channel_allowed_in_list(monkeypatch):
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [123])
    assert commands_mod._channel_allowed(123) is True


def test_channel_allowed_not_in_list(monkeypatch):
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [123])
    assert commands_mod._channel_allowed(456) is False


def test_bridge_sessions_file_constant_matches_bridge():
    from opencode_discord_bot import bridge as bridge_mod
    assert commands_mod._BRIDGE_SESSIONS_FILE == bridge_mod._BRIDGE_SESSIONS_FILE


def test_status_to_progress_text_busy():
    assert commands_mod._status_to_progress_text({"type": "busy"}) == "busy…"


def test_status_to_progress_text_retry_with_attempt():
    result = commands_mod._status_to_progress_text(
        {"type": "retry", "attempt": 2, "message": "rate limit"}
    )
    assert "attempt 2" in result
    assert "rate limit" in result


def test_status_to_progress_text_idle_returns_empty():
    assert commands_mod._status_to_progress_text({"type": "idle"}) == ""


def test_status_to_progress_text_unknown_returns_empty():
    assert commands_mod._status_to_progress_text({"type": "weird"}) == ""


# ---------------------------------------------------------------------------
# OpencodeBot instance helpers
# ---------------------------------------------------------------------------


def test_voice_attachment_returns_transcribable(bot_instance):
    msg = FakeMessage(attachments=[FakeAttachment(content_type="audio/ogg", filename="x.ogg")])
    result = bot_instance._voice_attachment(msg)
    assert result is not None


def test_voice_attachment_returns_none_for_text_message(bot_instance):
    msg = FakeMessage(attachments=[])
    assert bot_instance._voice_attachment(msg) is None


def test_resolve_sid_main_router(bot_instance):
    import asyncio
    asyncio.get_event_loop().run_until_complete(bot_instance.router.bind(123, "sid-main"))
    assert bot_instance._resolve_sid(123) == "sid-main"


def test_resolve_sid_none_when_unbound(bot_instance):
    assert bot_instance._resolve_sid(999) is None


def test_channel_ok_bound_channel(bot_instance):
    import asyncio
    asyncio.get_event_loop().run_until_complete(bot_instance.router.bind(555, "sid"))
    assert bot_instance._channel_ok(555) is True


def test_channel_ok_allowed_channel(monkeypatch, bot_instance):
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [777])
    assert bot_instance._channel_ok(777) is True


def test_channel_ok_unknown_channel(monkeypatch, bot_instance):
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [777])
    assert bot_instance._channel_ok(888) is False


def test_channel_ok_none_returns_false(bot_instance):
    assert bot_instance._channel_ok(None) is False


def test_is_guild_configured_all_defaults(bot_instance, monkeypatch):
    # All defaults: category=0, allowed=[], trigger=stale default → False.
    monkeypatch.setattr(config, "discord_bot_session_category_id", 0)
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [])
    monkeypatch.setattr(config, "voice_message_trigger_channel_id",
                        commands_mod._STALE_DEFAULT_TRIGGER_CHANNEL_ID)
    assert bot_instance._is_guild_configured() is False


def test_is_guild_configured_with_category(bot_instance, monkeypatch):
    monkeypatch.setattr(config, "discord_bot_session_category_id", 99)
    assert bot_instance._is_guild_configured() is True


# ---------------------------------------------------------------------------
# on_message 4-branch dispatch
# ---------------------------------------------------------------------------


async def test_on_message_ignores_bot_author(bot_instance):
    msg = FakeMessage(author=FakeAuthor(bot=True))
    msg.channel = FakeChannel()
    # Should return immediately — no helper called.
    bot_instance._run_followup = AsyncMock()
    await bot_instance.on_message(msg)
    bot_instance._run_followup.assert_not_called()


async def test_on_message_branch_d_ignored_for_unbound_non_voice(bot_instance):
    """Non-session channel + no voice attachment → ignored."""
    msg = FakeMessage(content="hello", channel=FakeChannel(id=999))
    bot_instance._run_followup = AsyncMock()
    await bot_instance.on_message(msg)
    bot_instance._run_followup.assert_not_called()


async def test_on_message_branch_b_text_followup(bot_instance, monkeypatch):
    """Session channel + text → _run_followup called."""
    import asyncio
    ch = FakeChannel(id=555)
    await bot_instance.router.bind(555, "sid-1")
    msg = FakeMessage(content="refine the plan", channel=ch)
    bot_instance._run_followup = AsyncMock()
    # Disable voice messages entirely so the voice branch is skipped.
    monkeypatch.setattr(config, "voice_message_enabled", False)
    await bot_instance.on_message(msg)
    bot_instance._run_followup.assert_awaited_once_with(msg, "sid-1")


async def test_on_message_branch_b_empty_content_ignored(bot_instance, monkeypatch):
    """Session channel + empty content → ignored (no follow-up)."""
    ch = FakeChannel(id=555)
    await bot_instance.router.bind(555, "sid-1")
    msg = FakeMessage(content="", channel=ch)
    bot_instance._run_followup = AsyncMock()
    monkeypatch.setattr(config, "voice_message_enabled", False)
    await bot_instance.on_message(msg)
    bot_instance._run_followup.assert_not_called()


async def test_on_message_branch_a_voice_followup(bot_instance, monkeypatch):
    """Session channel + voice attachment → _run_voice_followup called."""
    ch = FakeChannel(id=555)
    await bot_instance.router.bind(555, "sid-1")
    att = FakeAttachment(content_type="audio/ogg", filename="vm.ogg", data=b"")
    msg = FakeMessage(content="", channel=ch, attachments=[att])
    bot_instance._run_voice_followup = AsyncMock()
    await bot_instance.on_message(msg)
    bot_instance._run_voice_followup.assert_awaited_once()
    args = bot_instance._run_voice_followup.await_args
    assert args[0][1] == "sid-1"  # sid argument


async def test_on_message_branch_c_voice_message_trigger(bot_instance, monkeypatch):
    """Trigger channel + voice attachment → _run_talk_from_message called."""
    trigger_id = 888
    monkeypatch.setattr(config, "voice_message_trigger_channel_id", trigger_id)
    ch = FakeChannel(id=trigger_id)
    att = FakeAttachment(content_type="audio/ogg", filename="vm.ogg", data=b"")
    msg = FakeMessage(content="", channel=ch, attachments=[att])
    bot_instance._run_talk_from_message = AsyncMock()
    await bot_instance.on_message(msg)
    bot_instance._run_talk_from_message.assert_awaited_once()


async def test_on_message_yields_to_bridge_when_active(bot_instance, monkeypatch):
    """Bridge is active for sid → text follow-up yields silently."""
    from opencode_discord_bot import bridge_state as bs
    ch = FakeChannel(id=555)
    await bot_instance.router.bind(555, "sid-br")
    bs.mark_active("sid-br")
    msg = FakeMessage(content="reply", channel=ch)
    bot_instance._run_followup = AsyncMock()
    monkeypatch.setattr(config, "voice_message_enabled", False)
    await bot_instance.on_message(msg)
    bot_instance._run_followup.assert_not_called()


async def test_on_message_busy_session_rejected(bot_instance, monkeypatch):
    """Session already driving → "Session is busy" message + no follow-up."""
    ch = FakeChannel(id=555)
    await bot_instance.router.bind(555, "sid-busy")
    # Simulate an active drive.
    bot_instance._active_drives["sid-busy"] = asyncio.create_task(asyncio.sleep(100))
    msg = FakeMessage(content="more", channel=ch)
    bot_instance._run_followup = AsyncMock()
    monkeypatch.setattr(config, "voice_message_enabled", False)
    try:
        await bot_instance.on_message(msg)
        bot_instance._run_followup.assert_not_called()
        # Channel got a "busy" message.
        assert any("busy" in (c or "").lower() for c, _ in ch.sent)
    finally:
        bot_instance._active_drives["sid-busy"].cancel()
        try:
            await asyncio.gather(bot_instance._active_drives["sid-busy"], return_exceptions=True)
        except Exception:
            pass
        bot_instance._active_drives.pop("sid-busy", None)


# ---------------------------------------------------------------------------
# _run_followup — drive session with scripted opencode client
# ---------------------------------------------------------------------------


async def test_run_followup_drives_session_and_posts_final(bot_instance, monkeypatch):
    """Full follow-up: send_prompt_async + drive + final text posted to channel."""
    ch = FakeChannel(id=555)
    await bot_instance.router.bind(555, "sid-followup")
    msg = FakeMessage(id=10, content="refine the plan", channel=ch)
    # Script the opencode client: prompt sent, status busy→idle, final message.
    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("list_questions", [], [])  # no questions during drive
    bot_instance.client.script("list_permissions", [], [])
    bot_instance.client.script("get_session_status",
                                {"sid-followup": {"type": "busy"}},
                                {"sid-followup": {"type": "idle"}})
    bot_instance.client.script("list_messages", [assistant_message("FINAL PLAN", mid="m-1")])
    # list_messages for _fetch_last_user_message_id (before the drive).
    # _fetch_last_user_message_id retries up to 3x; script it to return a user msg.
    user_msg = {"info": {"role": "user", "id": "u-1"}, "parts": [{"type": "text", "text": "x"}]}
    # The first list_messages call is from _fetch_last_user_message_id, then
    # the drive's list_messages. Script enough to cover both.
    bot_instance.client.script("list_messages", [user_msg], [assistant_message("FINAL PLAN", mid="m-1")])

    monkeypatch.setattr(config, "voice_message_enabled", False)
    await bot_instance._run_followup(msg, "sid-followup")
    # The final text was posted to the channel.
    final_texts = [c for c, _ in ch.sent if c and "FINAL PLAN" in c]
    assert final_texts, f"final text not posted; sent: {[c for c,_ in ch.sent]}"