"""Chain A: Comulytic poll -> pull -> download -> transcribe -> route -> Discord -> questions -> final.

Real ffmpeg + faster-whisper on the sample clip; opencode/Discord/Comulytic
network boundaries are scripted fakes. A speaker-ID variant (guarded by
``require_pyannote``) runs real pyannote diarization on the same clip.
"""

import asyncio

import pytest

from opencode_discord_bot import bridge as bridge_mod
from opencode_discord_bot import bridge_state as bs
from opencode_discord_bot.config import config
from opencode_discord_bot.session_router import SessionRouter
from tests.fakes import (
    ScriptedOpencodeClient,
    ScriptedDiscordRest,
    ScriptedComulyticClient,
    assistant_message,
    discord_message_dict,
    question_request,
)


async def _script_opencode_for_one_question(opencode: ScriptedOpencodeClient, sid: str):
    """Wire the opencode fake for: create session, send prompt, one question
    surfaced+answered, busy->idle, final assistant message."""
    opencode.script("create_session", {"id": sid})
    opencode.script("send_prompt_async", None)
    opencode.script("get_session_status",
                    {sid: {"type": "busy"}},
                    {sid: {"type": "idle"}})
    opencode.script("list_questions",
                    [question_request(rid="q-1", sid=sid)],
                    [],
                    [])
    opencode.script("list_permissions", [], [], [])
    opencode.script("reply_question", True)
    opencode.script("list_messages", [assistant_message("FINAL PLAN: ship it", mid="m-1")])


async def _script_rest_for_one_reply(rest: ScriptedDiscordRest, expected_reply: str = "1"):
    """Wire the discord rest fake so the reply-poller sees a user reply."""
    # create_text_channel returns a dict with id (default).
    # create_message returns a dict with id (default).
    # list_messages: the question post returns msg id 1000; the reply comes
    # after it. Script list_messages to return the reply once, then empty.
    rest.script("list_messages",
                [discord_message_dict(mid=2000, content=expected_reply, author_id="9999")],
                [])


@pytest.fixture
def chain_config(monkeypatch, tmp_path):
    """Configure the bridge for a chain test (no real Discord token)."""
    monkeypatch.setattr(config, "discord_bot_token", "test-token")
    monkeypatch.setattr(config, "discord_bot_guild_id", 999)
    monkeypatch.setattr(config, "discord_bot_session_category_id", 0)
    monkeypatch.setattr(config, "comulytic_enabled", True)
    monkeypatch.setattr(config, "comulytic_jwt", "fake-jwt")
    monkeypatch.setattr(config, "comulytic_audio_path", "proxy")
    monkeypatch.setattr(config, "comulytic_state_file", str(tmp_path / "seen.json"))
    monkeypatch.setattr(config, "comulytic_plan_type", "")
    monkeypatch.setattr(config, "comulytic_question_timeout_seconds", 1.0)
    monkeypatch.setattr(config, "comulytic_question_poll_interval_seconds", 0.0)
    monkeypatch.setattr(config, "comulytic_poll_page_size", 20)
    monkeypatch.setattr(config, "comulytic_max_duration_hours", 0)
    monkeypatch.setattr(config, "comulytic_max_duration_minutes", 0)
    monkeypatch.setattr(config, "comulytic_max_duration_seconds", 0)
    monkeypatch.setattr(config, "voice_stt_provider", "local")
    monkeypatch.setattr(config, "voice_local_whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_device", "cpu")
    monkeypatch.setattr(config, "whisper_compute_type", "int8")
    monkeypatch.setattr(config, "speaker_id_enabled", False)
    return tmp_path


async def test_chain_comulytic_full_with_real_stt(
    chain_config, sample_mp3_bytes, stub_slug, tmp_path, monkeypatch
):
    """Chain A: Comulytic poll + pull + real STT + route to oc-assistant + Discord.

    Real ffmpeg + faster-whisper on the sample clip; fake Comulytic API
    returns the clip bytes; fake opencode REST scripts one question + a
    final response; fake Discord REST records the posted messages.
    """
    from opencode_discord_bot import voice as voice_mod
    voice_mod._LOCAL_WHISPER_MODEL = None  # force tiny model reload

    sid = "sid-chain-1"
    comulytic = ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)
    # probe_newest: total=1, newest="n-1" (new — not in seen).
    comulytic.script("probe_newest", (1, "n-1"))
    # list_recordings: page 1 returns the new recording (audio-delivered).
    comulytic.script("list_recordings",
                     {"data": {"data": [{"noteId": "n-1", "hasCloudAudio": True, "audioAccess": "public"}], "total": 1}})
    # get_note_detail for the recording.
    comulytic.script("get_note_detail",
                     {"hasCloudAudio": True, "audioAccess": "public",
                      "objStorageUrl": "https://s3.example/raw.mp3"})
    # download_audio_proxy returns the sample clip (default).
    comulytic.script("download_audio_proxy", sample_mp3_bytes)

    opencode = ScriptedOpencodeClient()
    await _script_opencode_for_one_question(opencode, sid)

    rest = ScriptedDiscordRest()
    await _script_rest_for_one_reply(rest, expected_reply="1")

    router = SessionRouter(tmp_path / "bridge-sessions.json")

    seen = set()  # bootstrapped=True so poll_once processes new recordings.
    bootstrapped = True
    result = await bridge_mod.poll_once(
        comulytic, opencode, seen, bootstrapped,
        str(chain_config / "seen.json"),
        rest=rest, router=router,
    )
    # The recording was processed.
    assert any(c[0] == "create_session" for c in opencode.calls)
    create_call = next(c for c in opencode.calls if c[0] == "create_session")
    assert "comulytic-n-1" in (create_call[2].get("title") or "")
    # A Discord channel was created.
    assert rest.created_channels
    # The transcript was posted to the channel.
    assert any("Transcribed prompt" in content for _, content in rest.posted)
    # send_prompt_async was called with agent="oc-assistant".
    prompt_calls = [c for c in opencode.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    assert prompt_calls[0][2]["agent"] == "oc-assistant"
    # The question was surfaced + answered (reply_question called).
    reply_calls = [c for c in opencode.calls if c[0] == "reply_question"]
    assert reply_calls
    # The final response was posted to the channel.
    assert any("FINAL PLAN" in content for _, content in rest.posted)


async def test_chain_comulytic_full_with_speaker_id(
    chain_config, sample_mp3_bytes, stub_slug, tmp_path, speakers_dir, require_pyannote, monkeypatch
):
    """Chain A (speaker-ID variant): real pyannote diarization on the sample clip.

    Skip when ``pyannote.audio`` isn't importable or ``HF_TOKEN`` is unset.
    Asserts the transcript posted to Discord is a string (the diarization
    output may or may not contain a speaker label depending on whether the
    single-speaker clip matches the Jake reference embeddings).
    """
    from opencode_discord_bot import voice as voice_mod
    from opencode_discord_bot import speakers as speakers_mod
    voice_mod._LOCAL_WHISPER_MODEL = None
    speakers_mod._SPEAKERS_CACHE = None
    speakers_mod._PYANNOTE_PIPELINE = None
    speakers_mod._SPEAKER_EMBEDDING = None
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    monkeypatch.setattr(config, "speakers_dir", str(speakers_dir))
    monkeypatch.setattr(config, "speaker_match_threshold", 0.5)

    sid = "sid-chain-2"
    comulytic = ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)
    comulytic.script("probe_newest", (1, "n-1"))
    comulytic.script("list_recordings",
                     {"data": {"data": [{"noteId": "n-1", "hasCloudAudio": True, "audioAccess": "public"}], "total": 1}})
    comulytic.script("get_note_detail",
                     {"hasCloudAudio": True, "audioAccess": "public",
                      "objStorageUrl": "https://s3.example/raw.mp3"})
    comulytic.script("download_audio_proxy", sample_mp3_bytes)

    opencode = ScriptedOpencodeClient()
    await _script_opencode_for_one_question(opencode, sid)

    rest = ScriptedDiscordRest()
    await _script_rest_for_one_reply(rest, expected_reply="1")

    router = SessionRouter(tmp_path / "bridge-sessions.json")

    seen = set()
    try:
        await bridge_mod.poll_once(
            comulytic, opencode, seen, True,
            str(chain_config / "seen.json"),
            rest=rest, router=router,
        )
        # The transcript posted to Discord is a non-empty string.
        transcript_posts = [c for _, c in rest.posted if "Transcribed prompt" in c]
        assert transcript_posts
        # The final response was posted.
        assert any("FINAL PLAN" in c for _, c in rest.posted)
    finally:
        voice_mod._LOCAL_WHISPER_MODEL = None
        speakers_mod._SPEAKERS_CACHE = None
        speakers_mod._PYANNOTE_PIPELINE = None
        speakers_mod._SPEAKER_EMBEDDING = None


async def test_chain_comulytic_log_only_no_discord(
    chain_config, sample_mp3_bytes, stub_slug, tmp_path, monkeypatch
):
    """Chain A (log-only variant): no Discord token → route logs the response."""
    from opencode_discord_bot import voice as voice_mod
    voice_mod._LOCAL_WHISPER_MODEL = None
    monkeypatch.setattr(config, "discord_bot_token", "")
    monkeypatch.setattr(config, "discord_bot_guild_id", 0)

    sid = "sid-chain-3"
    comulytic = ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)
    comulytic.script("probe_newest", (1, "n-1"))
    comulytic.script("list_recordings",
                     {"data": {"data": [{"noteId": "n-1", "hasCloudAudio": True, "audioAccess": "public"}], "total": 1}})
    comulytic.script("get_note_detail",
                     {"hasCloudAudio": True, "audioAccess": "public",
                      "objStorageUrl": "https://s3.example/raw.mp3"})
    comulytic.script("download_audio_proxy", sample_mp3_bytes)

    opencode = ScriptedOpencodeClient()
    opencode.script("create_session", {"id": sid})
    opencode.script("send_prompt_async", None)
    opencode.script("get_session_status",
                    {sid: {"type": "busy"}},
                    {sid: {"type": "idle"}})
    opencode.script("list_questions", [], [], [])
    opencode.script("list_permissions", [], [], [])
    opencode.script("list_messages", [assistant_message("LOG OUTPUT", mid="m-1")])

    router = SessionRouter(tmp_path / "bridge-sessions.json")
    seen = set()
    await bridge_mod.poll_once(
        comulytic, opencode, seen, True,
        str(chain_config / "seen.json"),
        rest=None, router=router,
    )
    # No Discord channel was created (log-only path).
    # send_prompt_async was called with oc-assistant.
    prompt_calls = [c for c in opencode.calls if c[0] == "send_prompt_async"]
    assert prompt_calls
    assert prompt_calls[0][2]["agent"] == "oc-assistant"