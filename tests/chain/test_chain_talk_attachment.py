"""Chain B: /oc_talk attachment -> extract audio -> transcribe -> (speakers) -> opencode -> questions -> final.

Uses the ``bot_instance`` fixture (no gateway) and a ``FakeAttachment``
whose ``.read()`` returns the real sample clip bytes. Real ffmpeg +
faster-whisper run; opencode REST is scripted.
"""

import asyncio

import pytest

from opencode_discord_bot import voice as voice_mod
from opencode_discord_bot import speakers as speakers_mod
from opencode_discord_bot.config import config
from tests.fakes import (
    FakeApplicationContext,
    FakeAttachment,
    FakeGuild,
    FakeChannel,
    assistant_message,
    question_request,
)


@pytest.fixture
def talk_config(monkeypatch):
    """Configure for /oc_talk chain: local tiny whisper, no speaker ID by default."""
    monkeypatch.setattr(config, "voice_stt_provider", "local")
    monkeypatch.setattr(config, "voice_local_whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_device", "cpu")
    monkeypatch.setattr(config, "whisper_compute_type", "int8")
    monkeypatch.setattr(config, "speaker_id_enabled", False)
    # Empty allowlist = all channels allowed (so _channel_ok returns True).
    monkeypatch.setattr(config, "discord_bot_allowed_channel_ids", [])
    monkeypatch.setattr(config, "discord_bot_session_category_id", 0)
    voice_mod._LOCAL_WHISPER_MODEL = None


async def _script_bot_for_talk(bot_instance, sid="sid-talk"):
    """Wire the bot's scripted opencode client for one prompt + final response."""
    bot_instance.client.script("create_session", {"id": sid})
    bot_instance.client.script("send_prompt_async", None)
    bot_instance.client.script("get_session_status",
                                {sid: {"type": "busy"}},
                                {sid: {"type": "idle"}})
    bot_instance.client.script("list_questions", [], [])
    bot_instance.client.script("list_permissions", [], [])
    bot_instance.client.script("list_messages", [assistant_message("TALK RESPONSE", mid="m-1")])


async def test_chain_talk_attachment_drives_session(
    bot_instance, talk_config, sample_mp3_bytes, monkeypatch
):
    """Full /oc_talk chain: real STT on the sample clip, opencode drive, final posted."""
    guild = FakeGuild()
    ctx = FakeApplicationContext(guild=guild)
    attachment = FakeAttachment(
        data=sample_mp3_bytes, content_type="audio/mpeg", filename="clip1.mp3"
    )
    await _script_bot_for_talk(bot_instance)

    await bot_instance._run_talk_session(ctx, attachment, plan_type=None)

    # The opencode session was created.
    create_calls = [c for c in bot_instance.client.calls if c[0] == "create_session"]
    assert create_calls
    # send_prompt_async was called with agent="oc-assistant".
    prompt_calls = [c for c in bot_instance.client.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    assert prompt_calls[0][2]["agent"] == "oc-assistant"
    # A session channel was created in the guild.
    assert guild.created_channels
    # The final response was posted to the created channel.
    ch = guild.created_channels[0]
    final_posts = [c for c, _ in ch.sent if c and "TALK RESPONSE" in c]
    assert final_posts, f"final not posted; sent: {[c for c,_ in ch.sent]}"


async def test_chain_talk_attachment_with_plan_type_directive(
    bot_instance, talk_config, sample_mp3_bytes, monkeypatch
):
    """/oc_talk with plan_type="note" prepends the directive to the prompt."""
    guild = FakeGuild()
    ctx = FakeApplicationContext(guild=guild)
    attachment = FakeAttachment(
        data=sample_mp3_bytes, content_type="audio/mpeg", filename="clip1.mp3"
    )
    await _script_bot_for_talk(bot_instance)

    await bot_instance._run_talk_session(ctx, attachment, plan_type="note")

    prompt_calls = [c for c in bot_instance.client.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    parts = prompt_calls[0][1][1]  # the parts list
    text = parts[0]["text"]
    assert "[PLAN_TYPE_PRESELECTED: note]" in text
    assert "[DISCORD_BOT]" in text


async def test_chain_talk_attachment_speaker_id_variant(
    bot_instance, talk_config, sample_mp3_bytes, speakers_dir, require_pyannote, monkeypatch
):
    """/oc_talk with speaker_id_enabled=True runs real pyannote diarization.

    Skip when pyannote/HF_TOKEN unavailable. Asserts the prompt was sent
    (the diarized transcript is the prompt text).
    """
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    monkeypatch.setattr(config, "speakers_dir", str(speakers_dir))
    monkeypatch.setattr(config, "speaker_match_threshold", 0.5)
    speakers_mod._SPEAKERS_CACHE = None
    speakers_mod._PYANNOTE_PIPELINE = None
    speakers_mod._SPEAKER_EMBEDDING = None
    voice_mod._LOCAL_WHISPER_MODEL = None

    guild = FakeGuild()
    ctx = FakeApplicationContext(guild=guild)
    attachment = FakeAttachment(
        data=sample_mp3_bytes, content_type="audio/mpeg", filename="clip1.mp3"
    )
    await _script_bot_for_talk(bot_instance, sid="sid-talk-speakers")

    try:
        await bot_instance._run_talk_session(ctx, attachment, plan_type=None)
        prompt_calls = [c for c in bot_instance.client.calls if c[0] == "send_prompt_async"]
        assert prompt_calls
        assert prompt_calls[0][2]["agent"] == "oc-assistant"
    finally:
        voice_mod._LOCAL_WHISPER_MODEL = None
        speakers_mod._SPEAKERS_CACHE = None
        speakers_mod._PYANNOTE_PIPELINE = None
        speakers_mod._SPEAKER_EMBEDDING = None


async def test_chain_talk_attachment_rejects_non_audio(bot_instance, talk_config, monkeypatch):
    """/oc_talk with a non-audio attachment returns early without creating a session."""
    from tests.fakes import FakeApplicationContext, FakeGuild
    guild = FakeGuild()
    ctx = FakeApplicationContext(guild=guild)
    # A text file — not transcribable.
    attachment = FakeAttachment(data=b"hello", content_type="text/plain", filename="notes.txt")
    await _script_bot_for_talk(bot_instance)
    await bot_instance._run_talk_session(ctx, attachment, plan_type=None)
    # No session created.
    create_calls = [c for c in bot_instance.client.calls if c[0] == "create_session"]
    assert not create_calls