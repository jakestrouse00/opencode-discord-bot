"""Unit tests for ``bridge.py`` — helpers + ``poll_once`` + ``route_to_plan_author``.

``_load_seen``/``_save_seen`` use real files at tmp_path. ``poll_once`` and
``route_to_plan_author`` use the scripted fakes. STT + ffmpeg run REAL on
the sample clip where a chain touches ``_transcribe_and_route``.
"""

import asyncio
import json

import pytest

from opencode_discord_bot import bridge as bridge_mod
from opencode_discord_bot.config import config
from opencode_discord_bot.session_router import SessionRouter
from tests.fakes import (
    ScriptedOpencodeClient,
    ScriptedDiscordRest,
    ScriptedComulyticClient,
    FakeChannel,
    assistant_message,
    discord_message_dict,
    question_request,
)


# ---------------------------------------------------------------------------
# _load_seen / _save_seen
# ---------------------------------------------------------------------------


def test_load_seen_missing_file_returns_empty(tmp_path):
    seen, boot = bridge_mod._load_seen(str(tmp_path / "absent.json"))
    assert seen == set()
    assert boot is False


def test_load_seen_empty_file_returns_empty(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("")
    seen, boot = bridge_mod._load_seen(str(p))
    assert seen == set()
    assert boot is False


def test_load_seen_round_trip(tmp_path):
    p = tmp_path / "seen.json"
    bridge_mod._save_seen(str(p), {"a", "b"}, True)
    seen, boot = bridge_mod._load_seen(str(p))
    assert seen == {"a", "b"}
    assert boot is True


def test_load_seen_corrupt_json_recovers(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("{not valid")
    seen, boot = bridge_mod._load_seen(str(p))
    assert seen == set()
    assert boot is False


def test_save_seen_writes_sorted_with_bootstrap_flag(tmp_path):
    p = tmp_path / "seen.json"
    bridge_mod._save_seen(str(p), {"z", "a", "m"}, True)
    data = json.loads(p.read_text())
    assert data["seen"] == ["a", "m", "z"]
    assert data["__bootstrapped__"] is True


# ---------------------------------------------------------------------------
# _enumerate_all_note_ids / _enumerate_new_note_ids
# ---------------------------------------------------------------------------


async def test_enumerate_all_note_ids_pages_through(monkeypatch):
    monkeypatch.setattr(config, "comulytic_poll_page_size", 20)
    client = ScriptedComulyticClient()
    # 25 total, page_size 20 → 2 pages.
    client.script("list_recordings",
                  {"data": {"data": [{"noteId": "1"}, {"noteId": "2"}], "total": 25}},
                  {"data": {"data": [{"noteId": "3"}], "total": 25}})
    ids = await bridge_mod._enumerate_all_note_ids(client, 25)
    assert ids == ["1", "2", "3"]


async def test_enumerate_new_note_ids_filters_seen(monkeypatch):
    monkeypatch.setattr(config, "comulytic_poll_page_size", 20)
    client = ScriptedComulyticClient()
    client.script("list_recordings",
                  {"data": {"data": [{"noteId": "1"}, {"noteId": "2"}], "total": 2}})
    new_ids, observed = await bridge_mod._enumerate_new_note_ids(client, 2, 20, {"1"})
    assert new_ids == ["2"]
    assert observed == ["1", "2"]


async def test_is_note_audio_delivered_cache_hit():
    client = ScriptedComulyticClient()
    # Pre-seed the cache with a delivered item.
    bridge_mod._PAGING_ITEM_CACHE["n-1"] = {"hasCloudAudio": True, "audioAccess": "public"}
    result = await bridge_mod._is_note_audio_delivered(client, "n-1")
    assert result is True
    # get_note_detail should NOT have been called.
    assert not any(c[0] == "get_note_detail" for c in client.calls)


async def test_is_note_audio_delivered_cache_miss_fetches_detail():
    client = ScriptedComulyticClient()
    client.script("get_note_detail", {"hasCloudAudio": True, "audioAccess": "public"})
    result = await bridge_mod._is_note_audio_delivered(client, "n-2")
    assert result is True
    assert any(c[0] == "get_note_detail" for c in client.calls)


# ---------------------------------------------------------------------------
# download_audio_smart
# ---------------------------------------------------------------------------


async def test_download_audio_smart_proxy_success(sample_mp3_bytes):
    client = ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)
    detail = {}
    result = await bridge_mod.download_audio_smart(client, "n-1", detail)
    assert result == sample_mp3_bytes
    # Path B was used.
    assert any(c[0] == "download_audio_proxy" for c in client.calls)


async def test_download_audio_smart_proxy_falls_back_to_presigned(sample_mp3_bytes, monkeypatch):
    client = ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)
    # Make proxy return empty so we fall through to Path A.
    client.script("download_audio_proxy", b"")
    # Path A needs a detail with an audio URL.
    detail = {"objStorageUrl": "https://s3.example/raw.mp3"}
    monkeypatch.setattr(config, "comulytic_audio_path", "proxy")
    result = await bridge_mod.download_audio_smart(client, "n-1", detail)
    assert result == sample_mp3_bytes
    assert any(c[0] == "download_audio_presigned" for c in client.calls)


async def test_download_audio_smart_presigned_403_remints(sample_mp3_bytes, monkeypatch):
    from opencode_discord_bot.comulytic import AudioUrlExpiredError
    client = ScriptedComulyticClient(default_audio_bytes=sample_mp3_bytes)
    # First presigned call raises expired; second succeeds.
    client.script_exc("download_audio_presigned", AudioUrlExpiredError("expired"))
    client.script("download_audio_presigned", sample_mp3_bytes)
    # Re-mint via get_note_detail.
    client.script("get_note_detail",
                  {"objStorageUrl": "https://s3.example/raw-v2.mp3"})
    detail = {"objStorageUrl": "https://s3.example/raw-v1.mp3"}
    monkeypatch.setattr(config, "comulytic_audio_path", "presigned")
    result = await bridge_mod.download_audio_smart(client, "n-1", detail)
    assert result == sample_mp3_bytes
    # get_note_detail was called for the re-mint.
    assert any(c[0] == "get_note_detail" for c in client.calls)


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------


async def test_poll_once_bootstrap_marks_all_seen_no_processing(tmp_seen_path, monkeypatch):
    """Bootstrap clause: marks all current recordings as seen, no processing."""
    monkeypatch.setattr(config, "comulytic_state_file", str(tmp_seen_path))
    monkeypatch.setattr(config, "comulytic_poll_page_size", 20)
    comulytic = ScriptedComulyticClient()
    comulytic.script("probe_newest", (5, "n-5"))
    # page_size=20 + total=5 → 1 page with all 5 notes.
    comulytic.script("list_recordings",
                     {"data": {"data": [{"noteId": "1"}, {"noteId": "2"}, {"noteId": "3"},
                                        {"noteId": "4"}, {"noteId": "5"}], "total": 5}})
    opencode = ScriptedOpencodeClient()
    seen = set()
    bootstrapped = await bridge_mod.poll_once(
        comulytic, opencode, seen, False, str(tmp_seen_path)
    )
    assert bootstrapped is True
    assert seen == {"1", "2", "3", "4", "5"}
    # No recordings were processed (process_new_recording not called).
    assert not any(c[0] == "create_session" for c in opencode.calls)


async def test_poll_once_fast_path_newest_in_seen(tmp_seen_path, monkeypatch):
    """Fast path: newest already in seen → no enumeration."""
    monkeypatch.setattr(config, "comulytic_state_file", str(tmp_seen_path))
    comulytic = ScriptedComulyticClient()
    comulytic.script("probe_newest", (5, "n-5"))
    opencode = ScriptedOpencodeClient()
    seen = {"n-5"}
    bootstrapped = await bridge_mod.poll_once(
        comulytic, opencode, seen, True, str(tmp_seen_path)
    )
    assert bootstrapped is True
    # No list_recordings (fast path short-circuits).
    assert not any(c[0] == "list_recordings" for c in comulytic.calls)


# ---------------------------------------------------------------------------
# route_to_plan_author (log-only path — no Discord configured)
# ---------------------------------------------------------------------------


async def test_route_to_plan_author_log_only_no_discord(monkeypatch):
    """rest=None → log-only path: create session, send prompt, poll, log."""
    monkeypatch.setattr(config, "discord_bot_token", "")
    monkeypatch.setattr(config, "discord_bot_guild_id", 0)
    monkeypatch.setattr(config, "comulytic_plan_type", "")
    opencode = ScriptedOpencodeClient()
    opencode.script("create_session", {"id": "sid-1"})
    opencode.script("send_prompt_async", None)
    opencode.script("get_session_status",
                    {"sid-1": {"type": "busy"}},
                    {"sid-1": {"type": "idle"}})
    opencode.script("list_messages", [assistant_message("PLAN OUTPUT", mid="m-1")])
    opencode.script("list_questions", [], [])
    opencode.script("list_permissions", [], [])
    result = await bridge_mod.route_to_plan_author(
        opencode, "transcript text", "n-1", rest=None, router=None
    )
    assert result["session_id"] == "sid-1"
    assert result["transcript"] == "transcript text"
    assert "PLAN OUTPUT" in result["response"]


async def test_route_to_plan_author_create_session_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "discord_bot_token", "")
    monkeypatch.setattr(config, "discord_bot_guild_id", 0)
    from opencode_discord_bot.opencode_client import OpencodeError
    opencode = ScriptedOpencodeClient()
    opencode.script_exc("create_session", OpencodeError("boom"))
    result = await bridge_mod.route_to_plan_author(
        opencode, "transcript", "n-1", rest=None, router=None
    )
    assert result["session_id"] is None
    assert result["response"] == ""