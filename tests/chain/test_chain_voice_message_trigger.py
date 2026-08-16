"""Chain C: voice message in trigger channel -> transcribe -> plan-author -> questions -> final.

Dispatches through ``bot.on_message`` so the full intake -> drive path is
exercised. Real STT on the sample clip; opencode REST scripted.
"""

import asyncio

import pytest

from opencode_discord_bot import voice as voice_mod
from opencode_discord_bot.config import config
from tests.fakes import (
    FakeChannel,
    FakeMessage,
    FakeAttachment,
    FakeAuthor,
    FakeGuild,
    assistant_message,
)


@pytest.fixture
def trigger_config(monkeypatch):
    """Configure the voice-message trigger channel + local tiny whisper."""
    monkeypatch.setattr(config, "voice_stt_provider", "local")
    monkeypatch.setattr(config, "voice_local_whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_device", "cpu")
    monkeypatch.setattr(config, "whisper_compute_type", "int8")
    monkeypatch.setattr(config, "speaker_id_enabled", False)
    monkeypatch.setattr(config, "voice_message_enabled", True)
    monkeypatch.setattr(config, "discord_bot_session_category_id", 0)
    voice_mod._LOCAL_WHISPER_MODEL = None


async def test_chain_voice_message_trigger_drives_new_session(
    bot_instance, trigger_config, sample_mp3_bytes, monkeypatch
):
    """Voice message in the trigger channel -> new plan-author session + final."""
    trigger_id = 8888
    monkeypatch.setattr(config, "voice_message_trigger_channel_id", trigger_id)
    guild = FakeGuild()
    trigger_channel = FakeChannel(id=trigger_id, name="voice-recordings", guild=guild)

    att = FakeAttachment(
        data=sample_mp3_bytes, content_type="audio/ogg", filename="voice-message.ogg"
    )
    msg = FakeMessage(
        id=1, content="", channel=trigger_channel,
        author=FakeAuthor(id=100, name="user"),
        attachments=[att], guild=guild,
    )

    sid = "sid-vm-1"
    bot_instance.client.script("create_session", {"id": sid})
    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("get_session_status",
                                {sid: {"type": "busy"}},
                                {sid: {"type": "idle"}})
    bot_instance.client.script("list_questions", [], [])
    bot_instance.client.script("list_permissions", [], [])
    bot_instance.client.script("list_messages", [assistant_message("VM PLAN", mid="m-1")])

    await bot_instance.on_message(msg)

    # A new session was created.
    create_calls = [c for c in bot_instance.client.calls if c[0] == "create_session"]
    assert create_calls
    # send_prompt_async with plan-author.
    prompt_calls = [c for c in bot_instance.client.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    assert prompt_calls[0][2]["agent"] == "plan-author"
    # A new channel was created in the guild.
    assert guild.created_channels
    # The trigger channel got a pointer message.
    pointer_msgs = [c for c, _ in trigger_channel.sent if c and "Created" in c]
    assert pointer_msgs
    # The new channel got the final response.
    new_ch = guild.created_channels[0]
    final_posts = [c for c, _ in new_ch.sent if c and "VM PLAN" in c]
    assert final_posts, f"final not posted; sent: {[c for c,_ in new_ch.sent]}"


async def test_chain_voice_message_in_session_channel_followup(
    bot_instance, trigger_config, sample_mp3_bytes, monkeypatch
):
    """Voice message in an already-bound session channel -> voice follow-up."""
    import asyncio
    # Bind a session channel.
    sid = "sid-existing"
    session_ch = FakeChannel(id=555, name="existing-session", guild=FakeGuild())
    await bot_instance.router.bind(555, sid)

    att = FakeAttachment(
        data=sample_mp3_bytes, content_type="audio/ogg", filename="vm.ogg"
    )
    msg = FakeMessage(
        id=2, content="", channel=session_ch,
        author=FakeAuthor(id=100, name="user"),
        attachments=[att], guild=session_ch.guild,
    )

    # Script opencode for the follow-up drive.
    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("get_session_status",
                                {sid: {"type": "busy"}},
                                {sid: {"type": "idle"}})
    bot_instance.client.script("list_questions", [], [])
    bot_instance.client.script("list_permissions", [], [])
    # _fetch_last_user_message_id polls list_messages; the drive's list_messages
    # returns the final assistant. Script enough entries.
    user_msg = {"info": {"role": "user", "id": "u-1"}, "parts": [{"type": "text", "text": "x"}]}
    bot_instance.client.script("list_messages",
                                [user_msg],
                                [assistant_message("FOLLOWUP REPLY", mid="m-1")])

    await bot_instance.on_message(msg)

    # send_prompt_async was called (no agent override for follow-ups).
    prompt_calls = [c for c in bot_instance.client.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    assert prompt_calls[0][2]["agent"] is None
    # The final reply was posted to the session channel.
    final_posts = [c for c, _ in session_ch.sent if c and "FOLLOWUP REPLY" in c]
    assert final_posts