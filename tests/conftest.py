"""Shared pytest fixtures for the opencode-discord-bot test suite.

Design notes:
- ``isolated_config`` is autouse: snapshots the ``config`` singleton's
  ``__dict__`` before each test and restores after, so tests can mutate
  ``config.<field>`` freely without bleeding across tests.
- ``no_serve`` patches ``OpencodeServe.start``/``stop`` to no-ops so
  importing or constructing ``OpencodeBot`` never spawns a subprocess.
  Also sets ``config.opencode_serve_enabled=False`` +
  ``config.comulytic_enabled=False`` to skip the bridge auto-spawn.
- ``stub_slug`` patches ``slug.generate_slug`` AND ``bridge.generate_slug``
  (the bridge imports it by name) to return a canned slug — prevents any
  httpx/Ollama call during chain tests.
- ``sample_mp3_bytes`` / ``sample_wav_bytes`` are session-scoped: the
  real ffmpeg + faster-whisper pass runs ONCE per test session and is
  cached. Tests that mock STT entirely don't need them.
- ``speakers_dir`` resolves the committed ``Speakers/`` folder; speaker-ID
  tests use it for ``load_speakers``.
- ``require_pyannote`` is a guard fixture that skips when
  ``pyannote.audio`` isn't importable OR ``HF_TOKEN`` is unset (the
  ~1-2GB first-run download needs the token + license acceptance).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from opencode_discord_bot.config import config
from opencode_discord_bot import bridge as bridge_module
from opencode_discord_bot import slug as slug_module
from opencode_discord_bot import speakers as speakers_module
from opencode_discord_bot import voice as voice_module
from opencode_discord_bot import bridge_state as bridge_state_module

from tests.fakes import (
    ScriptedOpencodeClient,
    ScriptedDiscordRest,
    ScriptedComulyticClient,
)


# ---------------------------------------------------------------------------
# Config isolation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_config():
    """Snapshot/restore the ``config`` singleton around each test."""
    saved = dict(config.__dict__)
    saved_model_config = dict(config.model_config)
    yield
    config.__dict__.clear()
    config.__dict__.update(saved)
    config.model_config.clear()
    config.model_config.update(saved_model_config)


@pytest.fixture(autouse=True)
def clean_bridge_state():
    """Reset bridge_state + the bridge paging cache + voice singletons."""
    bridge_state_module._active_sids.clear()
    bridge_module._clear_paging_cache()
    # Reset speakers cache so a test's load_speakers() re-reads.
    speakers_module._SPEAKERS_CACHE = None
    yield
    bridge_state_module._active_sids.clear()
    bridge_module._clear_paging_cache()
    speakers_module._SPEAKERS_CACHE = None
    # Voice/whisper singletons are heavy + per-process; leave them.


# ---------------------------------------------------------------------------
# Path fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_router_path(tmp_path):
    """Return a tmp file path for a real-file ``SessionRouter``."""
    return tmp_path / "sessions.json"


@pytest.fixture
def tmp_seen_path(tmp_path):
    """Return a tmp file path for the bridge seen-set."""
    return tmp_path / "seen.json"


@pytest.fixture
def tmp_bridge_router_path(tmp_path):
    """Return a tmp file path for the bridge's SessionRouter."""
    return tmp_path / "bridge-sessions.json"


# ---------------------------------------------------------------------------
# Sample audio (session-scoped — real ffmpeg + STT run ONCE)
# ---------------------------------------------------------------------------

BOT_DIR = Path(__file__).resolve().parent.parent
SPEAKERS_DIR = BOT_DIR / "Speakers"
SAMPLE_CLIP = SPEAKERS_DIR / "Jake" / "clip1.mp3"


@pytest.fixture(scope="session")
def sample_mp3_bytes():
    """Bytes of the committed ``Speakers/Jake/clip1.mp3`` (~59KB)."""
    if not SAMPLE_CLIP.exists():
        pytest.skip(f"sample clip not found at {SAMPLE_CLIP}")
    return SAMPLE_CLIP.read_bytes()


@pytest.fixture(scope="session")
def sample_wav_bytes(sample_mp3_bytes):
    """Real ffmpeg output: the sample clip normalized to mono 16kHz WAV."""
    import asyncio
    return asyncio.run(
        voice_module.extract_audio_to_wav(
            sample_mp3_bytes, content_type="audio/mpeg", filename="clip1.mp3"
        )
    )


@pytest.fixture(scope="session")
def sample_audio_duration(sample_mp3_bytes):
    """Real ffprobe duration of the sample clip (seconds)."""
    import asyncio
    return asyncio.run(
        voice_module.probe_audio_duration_seconds(
            sample_mp3_bytes, filename="clip1.mp3"
        )
    )


@pytest.fixture
def speakers_dir():
    """Path to the committed ``Speakers/`` folder (skip if absent)."""
    if not SPEAKERS_DIR.is_dir():
        pytest.skip(f"Speakers/ folder not found at {SPEAKERS_DIR}")
    return SPEAKERS_DIR


# ---------------------------------------------------------------------------
# pyannote guard
# ---------------------------------------------------------------------------


@pytest.fixture
def require_pyannote():
    """Skip the test if pyannote.audio isn't importable OR HF_TOKEN is unset."""
    try:
        importlib.import_module("pyannote.audio")
    except ImportError:
        pytest.skip("pyannote.audio not installed — speaker-ID test skipped")
    if not config.hf_token:
        pytest.skip("HF_TOKEN not set — pyannote models can't be downloaded")


# ---------------------------------------------------------------------------
# Stubbing fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def no_serve(monkeypatch):
    """Patch ``OpencodeServe.start``/``stop`` to no-ops; disable serve + bridge."""
    from opencode_discord_bot import opencode_serve as serve_module
    monkeypatch.setattr(serve_module.OpencodeServe, "start", lambda self, *a, **kw: True)
    monkeypatch.setattr(serve_module.OpencodeServe, "stop", lambda self, *a, **kw: None)
    monkeypatch.setattr(config, "opencode_serve_enabled", False)
    monkeypatch.setattr(config, "comulytic_enabled", False)
    monkeypatch.setattr(config, "discord_bot_token", "")  # no bridge auto-spawn


@pytest.fixture
def stub_slug(monkeypatch):
    """Patch ``generate_slug`` everywhere it's imported by name."""
    canned = "test-slug"

    async def fake_generate_slug(prompt, fallback=""):
        return canned

    monkeypatch.setattr(slug_module, "generate_slug", fake_generate_slug)
    # bridge.py imports generate_slug by name at module top.
    monkeypatch.setattr(bridge_module, "generate_slug", fake_generate_slug)
    return canned


@pytest.fixture
def stub_tts(monkeypatch):
    """Patch ``synthesize_speech`` + ``_openai_client`` to no-ops."""
    async def _fake_synthesize(text):
        return b"mp3-bytes"
    monkeypatch.setattr(voice_module, "synthesize_speech", _fake_synthesize)

    def fake_openai_client():
        class _Fake:
            class audio:
                class speech:
                    @staticmethod
                    async def create(**kw):
                        class _R:
                            content = b"mp3-bytes"
                        return _R()
                class transcriptions:
                    @staticmethod
                    async def create(**kw):
                        class _R:
                            text = "stub transcription"
                        return _R()
        return _Fake()

    monkeypatch.setattr(voice_module, "_openai_client", fake_openai_client)


# ---------------------------------------------------------------------------
# Scripted fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_opencode():
    """A fresh ``ScriptedOpencodeClient`` for each test."""
    return ScriptedOpencodeClient()


@pytest.fixture
def fake_rest():
    """A fresh ``ScriptedDiscordRest`` for each test."""
    return ScriptedDiscordRest()


@pytest.fixture
def fake_comulytic(sample_mp3_bytes):
    """A fresh ``ScriptedComulyticClient`` (audio defaults to the sample clip)."""
    return ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)


# ---------------------------------------------------------------------------
# OpencodeBot instance (no gateway, no serve, no bridge)
# ---------------------------------------------------------------------------


@pytest.fixture
async def bot_instance(no_serve, tmp_router_path, stub_slug, fake_opencode, monkeypatch):
    """Construct ``OpencodeBot`` with all network surfaces replaced by fakes.

    ``bot.client`` is the scripted opencode client; ``bot.router`` is a
    real ``SessionRouter`` at a tmp path; ``bot._serve`` is a ``MagicMock``;
    ``bot.bridge_router`` is left ``None`` (lazy). The bot is never started.

    Async because Pycord's ``discord.Bot.__init__`` calls
    ``asyncio.get_event_loop()`` which needs a running loop (provided by
    pytest-asyncio's auto mode for async fixtures).
    """
    from unittest.mock import MagicMock
    from opencode_discord_bot.commands import OpencodeBot
    from opencode_discord_bot.session_router import SessionRouter

    bot = OpencodeBot()
    bot.client = fake_opencode
    bot.router = SessionRouter(tmp_router_path)
    bot._serve = MagicMock()
    bot.bridge_router = None
    # Don't let _run_prompt try to resolve a real category.
    monkeypatch.setattr(config, "discord_bot_session_category_id", 0)
    return bot