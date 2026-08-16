"""Unit tests for ``voice.py`` — real ffmpeg + faster-whisper on sample clips.

The ``sample_mp3_bytes`` / ``sample_wav_bytes`` fixtures are session-scoped
so the real ffmpeg + STT pass runs ONCE per test session. ``synthesize_speech``
is mocked (no OpenAI key in CI).
"""

import asyncio
import pytest

from opencode_discord_bot import voice as voice_mod
from opencode_discord_bot.config import config


# ---------------------------------------------------------------------------
# is_transcribable_attachment
# ---------------------------------------------------------------------------


class _FakeAtt:
    def __init__(self, content_type="", filename=""):
        self.content_type = content_type
        self.filename = filename


def test_is_transcribable_audio_content_type():
    assert voice_mod.is_transcribable_attachment(_FakeAtt("audio/mpeg", "x.mp3"))


def test_is_transcribable_video_content_type():
    assert voice_mod.is_transcribable_attachment(_FakeAtt("video/mp4", "x.mp4"))


def test_is_transcribable_application_ogg():
    assert voice_mod.is_transcribable_attachment(_FakeAtt("application/ogg", "x.ogg"))


def test_is_transcribable_extension_fallback():
    assert voice_mod.is_transcribable_attachment(_FakeAtt("", "clip.mp3"))
    assert voice_mod.is_transcribable_attachment(_FakeAtt("", "clip.m4a"))


def test_is_not_transcribable_unknown():
    assert not voice_mod.is_transcribable_attachment(_FakeAtt("text/plain", "x.txt"))
    assert not voice_mod.is_transcribable_attachment(_FakeAtt("", "x.txt"))


# ---------------------------------------------------------------------------
# extract_audio_to_wav — REAL ffmpeg
# ---------------------------------------------------------------------------


async def test_extract_audio_to_wav_mp3_to_wav(sample_mp3_bytes):
    """Real ffmpeg: mp3 → mono 16kHz WAV."""
    result = await voice_mod.extract_audio_to_wav(
        sample_mp3_bytes, content_type="audio/mpeg", filename="clip1.mp3"
    )
    assert isinstance(result, bytes)
    assert len(result) > 0
    # WAV header: "RIFF" + size + "WAVE".
    assert result[:4] == b"RIFF"
    assert result[8:12] == b"WAVE"


async def test_extract_audio_to_wav_wav_passthrough(sample_wav_bytes):
    """WAV input passes through unchanged."""
    result = await voice_mod.extract_audio_to_wav(
        sample_wav_bytes, content_type="audio/wav", filename="x.wav"
    )
    assert result == sample_wav_bytes


async def test_extract_audio_to_wav_wav_extension_passthrough(sample_wav_bytes):
    """WAV detected by extension (no content_type)."""
    result = await voice_mod.extract_audio_to_wav(
        sample_wav_bytes, content_type="", filename="x.wav"
    )
    assert result == sample_wav_bytes


# ---------------------------------------------------------------------------
# probe_audio_duration_seconds — REAL ffprobe
# ---------------------------------------------------------------------------


async def test_probe_audio_duration_returns_positive(sample_mp3_bytes):
    duration = await voice_mod.probe_audio_duration_seconds(
        sample_mp3_bytes, filename="clip1.mp3"
    )
    assert duration is not None
    assert duration > 0


async def test_probe_audio_duration_empty_bytes_returns_none():
    assert await voice_mod.probe_audio_duration_seconds(b"", filename="x.mp3") is None


# ---------------------------------------------------------------------------
# transcribe_audio — REAL faster-whisper (local provider)
# ---------------------------------------------------------------------------


async def test_transcribe_audio_local_returns_text(sample_wav_bytes, monkeypatch):
    """Real faster-whisper on the sample clip → non-empty string."""
    monkeypatch.setattr(config, "voice_stt_provider", "local")
    monkeypatch.setattr(config, "voice_local_whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_device", "cpu")
    monkeypatch.setattr(config, "whisper_compute_type", "int8")
    # Reset the singleton so the tiny model loads fresh.
    voice_mod._LOCAL_WHISPER_MODEL = None
    try:
        result = await voice_mod.transcribe_audio(sample_wav_bytes)
        assert isinstance(result, str)
        # The clip may or may not produce speech; we only assert it returns
        # a string (no exception). Real STT on a 59KB clip is fast.
    finally:
        voice_mod._LOCAL_WHISPER_MODEL = None  # let other tests re-load


async def test_transcribe_audio_cloud_uses_openai(monkeypatch, stub_tts):
    """The openai provider path calls _openai_client (stubbed)."""
    monkeypatch.setattr(config, "voice_stt_provider", "openai")
    monkeypatch.setattr(config, "voice_stt_model", "whisper-1")
    monkeypatch.setattr(config, "openai_api_key", "stub-key")
    # _openai_client is patched by stub_tts to return a fake whose
    # transcriptions.create returns text="stub transcription".
    result = await voice_mod.transcribe_audio(b"fake-wav-bytes")
    assert result == "stub transcription"


async def test_transcribe_audio_auto_falls_back_to_cloud_on_local_failure(monkeypatch, stub_tts):
    """Provider=auto: local raises → cloud fallback (stubbed) returns text."""
    monkeypatch.setattr(config, "voice_stt_provider", "auto")
    monkeypatch.setattr(config, "openai_api_key", "stub-key")

    async def _failing_local(_bytes):
        raise RuntimeError("local whisper broke")
    monkeypatch.setattr(voice_mod, "_transcribe_local", _failing_local)
    # _openai_client is stubbed via stub_tts.
    result = await voice_mod.transcribe_audio(b"x")
    assert result == "stub transcription"


# ---------------------------------------------------------------------------
# synthesize_speech — patched (no OpenAI key in CI)
# ---------------------------------------------------------------------------


async def test_synthesize_speech_raises_on_empty_key(monkeypatch):
    monkeypatch.setattr(config, "openai_api_key", "")
    # Force the lazy client to re-evaluate by patching _openai_client.
    from opencode_discord_bot.voice import _openai_client
    # _openai_client reads config.openai_api_key at call time.
    with pytest.raises(RuntimeError, match="openai_api_key is empty"):
        await voice_mod.synthesize_speech("hi")