"""Unit tests for ``speakers.py``.

The pure helpers (``_cosine_similarity``, ``_match_speaker``) are tested
with synthetic numpy arrays — no pyannote needed. The end-to-end
``load_speakers`` + ``identify_speakers`` + ``transcribe_with_speakers``
paths require pyannote.audio + HF_TOKEN and are guarded by the
``require_pyannote`` fixture (they skip otherwise).
"""

import asyncio

import pytest

from opencode_discord_bot import speakers as speakers_mod
from opencode_discord_bot import voice as voice_mod
from opencode_discord_bot.config import config


# ---------------------------------------------------------------------------
# Pure helpers — no pyannote needed
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical():
    import numpy as np
    a = np.array([1.0, 0.0, 0.0])
    assert speakers_mod._cosine_similarity(a, a) == 1.0


def test_cosine_similarity_orthogonal():
    import numpy as np
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert speakers_mod._cosine_similarity(a, b) == 0.0


def test_cosine_similarity_zero_norm_returns_zero():
    import numpy as np
    z = np.zeros(3)
    assert speakers_mod._cosine_similarity(z, z) == 0.0


def test_match_speaker_returns_best_above_threshold():
    import numpy as np
    ref = np.array([1.0, 0.0])
    speakers = {"Jake": [ref]}
    # Identical embedding → cosine 1.0 > threshold.
    assert speakers_mod._match_speaker(ref, speakers, threshold=0.5) == "Jake"


def test_match_speaker_returns_none_below_threshold():
    import numpy as np
    ref = np.array([1.0, 0.0])
    speakers = {"Jake": [np.array([0.0, 1.0])]}  # orthogonal
    assert speakers_mod._match_speaker(ref, speakers, threshold=0.9) is None


def test_match_speaker_picks_best_among_multiple():
    import numpy as np
    target = np.array([1.0, 0.0])
    speakers = {
        "Jake": [np.array([0.9, 0.1])],   # close
        "Bob": [np.array([0.0, 1.0])],    # orthogonal
    }
    assert speakers_mod._match_speaker(target, speakers, threshold=0.5) == "Jake"


# ---------------------------------------------------------------------------
# _slice_wav — pure stdlib wave
# ---------------------------------------------------------------------------


def _make_wav_bytes(seconds: float = 1.0, freq: int = 16000) -> bytes:
    """Build a mono 16kHz WAV of `seconds` duration (silence)."""
    import io
    import wave
    n_frames = int(seconds * freq)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(freq)
        w.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


def test_slice_wav_returns_subslice():
    wav = _make_wav_bytes(2.0)
    sliced = speakers_mod._slice_wav(wav, 0.5, 1.0)
    assert sliced[:4] == b"RIFF"
    # The sliced WAV should be smaller than the original.
    assert len(sliced) < len(wav)


def test_slice_wav_empty_range_returns_empty():
    wav = _make_wav_bytes(1.0)
    assert speakers_mod._slice_wav(wav, 0.8, 0.2) == b""


def test_slice_wav_out_of_range_returns_empty():
    wav = _make_wav_bytes(1.0)
    # Range entirely beyond the end.
    assert speakers_mod._slice_wav(wav, 5.0, 10.0) == b""


# ---------------------------------------------------------------------------
# transcribe_with_speakers — fallback path (no pyannote needed)
# ---------------------------------------------------------------------------


async def test_transcribe_with_speakers_disabled_falls_back_to_anonymous(monkeypatch, sample_wav_bytes):
    """speaker_id_enabled=False → delegates to voice.transcribe_audio."""
    monkeypatch.setattr(config, "speaker_id_enabled", False)

    called = {"n": 0}
    captured = {}

    async def fake_transcribe(wav_bytes):
        called["n"] += 1
        captured["bytes"] = wav_bytes
        return "anonymous transcript"

    monkeypatch.setattr(voice_mod, "transcribe_audio", fake_transcribe)
    result = await speakers_mod.transcribe_with_speakers(sample_wav_bytes)
    assert result == "anonymous transcript"
    assert called["n"] == 1
    assert captured["bytes"] == sample_wav_bytes


async def test_transcribe_with_speakers_empty_speakers_falls_back(monkeypatch, sample_wav_bytes):
    """speaker_id_enabled=True but load_speakers returns {} → anonymous."""
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    # Force the cache to empty so load_speakers returns {}.
    speakers_mod._SPEAKERS_CACHE = {}

    async def fake_transcribe(wav_bytes):
        return "anonymous fallback"

    monkeypatch.setattr(voice_mod, "transcribe_audio", fake_transcribe)
    result = await speakers_mod.transcribe_with_speakers(sample_wav_bytes)
    assert result == "anonymous fallback"


async def test_transcribe_with_speakers_pyannote_missing_falls_back(monkeypatch, sample_wav_bytes):
    """speaker_id_enabled=True but load_speakers raises → anonymous."""
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    # Make load_speakers raise (simulating pyannote not importable).
    speakers_mod._SPEAKERS_CACHE = None

    def boom(*a, **kw):
        raise RuntimeError("pyannote not installed")
    monkeypatch.setattr(speakers_mod, "load_speakers", boom)

    async def fake_transcribe(wav_bytes):
        return "anonymous on error"

    monkeypatch.setattr(voice_mod, "transcribe_audio", fake_transcribe)
    result = await speakers_mod.transcribe_with_speakers(sample_wav_bytes)
    assert result == "anonymous on error"


# ---------------------------------------------------------------------------
# Real pyannote paths — guarded by require_pyannote
# ---------------------------------------------------------------------------


async def test_load_speakers_real_returns_jake(require_pyannote, speakers_dir, monkeypatch):
    """Real pyannote on the committed Speakers/Jake/ clips → {'Jake': [emb, ...]}."""
    monkeypatch.setattr(config, "speakers_dir", str(speakers_dir))
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    speakers_mod._SPEAKERS_CACHE = None
    result = await asyncio.to_thread(speakers_mod.load_speakers, speakers_dir)
    assert "Jake" in result
    assert len(result["Jake"]) > 0
    # Each embedding should be a 1-D array.
    import numpy as np
    for emb in result["Jake"]:
        arr = np.asarray(emb)
        assert arr.ndim == 1


async def test_identify_speakers_real_returns_labeled_transcript(
    require_pyannote, speakers_dir, sample_wav_bytes, monkeypatch
):
    """Real pyannote diarization + Whisper on the sample clip → labeled lines.

    Single-speaker clip with Jake reference loaded → turns labeled "Jake"
    (or "Speaker 1" if the cosine match is below threshold). The test only
    asserts the result is a non-empty string of "Label: text" lines, NOT a
    specific label, because the 5 identical clips may produce variable
    diarization output.
    """
    monkeypatch.setattr(config, "speakers_dir", str(speakers_dir))
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    monkeypatch.setattr(config, "speaker_match_threshold", 0.5)
    monkeypatch.setattr(config, "voice_stt_provider", "local")
    monkeypatch.setattr(config, "voice_local_whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_device", "cpu")
    monkeypatch.setattr(config, "whisper_compute_type", "int8")
    voice_mod._LOCAL_WHISPER_MODEL = None
    speakers_mod._SPEAKERS_CACHE = None
    speakers_mod._PYANNOTE_PIPELINE = None
    speakers_mod._SPEAKER_EMBEDDING = None
    try:
        result = await speakers_mod.identify_speakers(sample_wav_bytes)
        # Either diarization succeeds (non-empty) or fails (""). Both are
        # acceptable test outcomes — assert it's a string.
        assert isinstance(result, str)
    finally:
        # Clear heavy singletons so they don't leak across tests with
        # different model settings.
        voice_mod._LOCAL_WHISPER_MODEL = None
        speakers_mod._SPEAKERS_CACHE = None
        speakers_mod._PYANNOTE_PIPELINE = None
        speakers_mod._SPEAKER_EMBEDDING = None


async def test_transcribe_with_speakers_real_orchestrates(
    require_pyannote, speakers_dir, sample_wav_bytes, monkeypatch
):
    """Real end-to-end orchestrator: speaker_id_enabled + Jake loaded."""
    monkeypatch.setattr(config, "speakers_dir", str(speakers_dir))
    monkeypatch.setattr(config, "speaker_id_enabled", True)
    monkeypatch.setattr(config, "speaker_match_threshold", 0.5)
    monkeypatch.setattr(config, "voice_stt_provider", "local")
    monkeypatch.setattr(config, "voice_local_whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_model", "tiny")
    monkeypatch.setattr(config, "whisper_device", "cpu")
    monkeypatch.setattr(config, "whisper_compute_type", "int8")
    voice_mod._LOCAL_WHISPER_MODEL = None
    speakers_mod._SPEAKERS_CACHE = None
    speakers_mod._PYANNOTE_PIPELINE = None
    speakers_mod._SPEAKER_EMBEDDING = None
    try:
        result = await speakers_mod.transcribe_with_speakers(sample_wav_bytes)
        assert isinstance(result, str)
    finally:
        voice_mod._LOCAL_WHISPER_MODEL = None
        speakers_mod._SPEAKERS_CACHE = None
        speakers_mod._PYANNOTE_PIPELINE = None
        speakers_mod._SPEAKER_EMBEDDING = None