"""Unit tests for ``dashboard_state.py`` — the in-process shared state.

All state is module-level and shared, so an autouse fixture resets it
between tests (the module's own ``reset()`` is exactly what a process
restart does to this state).
"""

import asyncio
import logging

import pytest

from opencode_discord_bot import dashboard_state as ds


@pytest.fixture(autouse=True)
def _reset_state():
    ds.reset()
    yield
    ds.reset()


def test_default_flags_off():
    assert ds.is_skipping_transcription() is False
    assert ds.is_paused() is False
    assert ds.pending_action() is None
    assert ds.in_flight_note_id() is None


def test_skip_transcription_setter():
    ds.set_skip_transcription(True)
    assert ds.is_skipping_transcription() is True
    ds.set_skip_transcription(False)
    assert ds.is_skipping_transcription() is False


def test_paused_setter():
    ds.set_paused(True)
    assert ds.is_paused() is True
    ds.set_paused(False)
    assert ds.is_paused() is False


def test_counters():
    ds.record_processed()
    ds.record_processed()
    ds.record_skipped()
    ds.record_skipped(11)
    ds.record_failed()
    snap = ds.snapshot()
    assert snap["processed"] == 2
    assert snap["skipped"] == 12
    assert snap["failed"] == 1


def test_pending_action_round_trip():
    ds.request_action("mark_all_seen")
    assert ds.pending_action() == "mark_all_seen"
    assert ds.take_pending_action() == "mark_all_seen"
    # Taken == cleared.
    assert ds.take_pending_action() is None
    assert ds.pending_action() is None


def test_register_seen_and_snapshot():
    seen = {"a", "b", "c"}
    ds.register_seen(seen)
    snap = ds.snapshot()
    assert snap["seen_count"] == 3
    # Read-only reference: external mutation shows through (it's the live set).
    seen.add("d")
    assert ds.snapshot()["seen_count"] == 4


def test_history_entries():
    ds.add_history("n-1", "processed", 12.34)
    entries = ds.snapshot()["recent"]
    assert entries[-1]["note_id"] == "n-1"
    assert entries[-1]["status"] == "processed"
    assert entries[-1]["seconds"] == 12.3


def test_poll_result():
    ds.set_poll_result("ok")
    snap = ds.snapshot()
    assert snap["last_poll_status"] == "ok"
    assert snap["last_poll_at"] is not None


async def test_in_flight_abort():
    started = False

    async def worker():
        nonlocal started
        started = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(worker())
    ds.set_in_flight(task, "n-1")
    assert ds.in_flight_note_id() == "n-1"
    # Give the worker a chance to actually start.
    await asyncio.sleep(0)
    assert started is True
    assert ds.abort_in_flight() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    ds.clear_in_flight()
    assert ds.in_flight_note_id() is None
    # Abort with nothing in flight is a no-op.
    assert ds.abort_in_flight() is False


def test_ring_handler_captures_and_filters():
    ds.install_log_handler()
    try:
        # Explicit child levels: the record-level check happens at the
        # emitting logger, so INFO passes once the child allows it (in
        # production the root logger is configured at INFO by the bot).
        logging.getLogger("comulytic.bridge").setLevel(logging.INFO)
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
        logging.getLogger("comulytic.bridge").info("hello from bridge")
        logging.getLogger("uvicorn.access").info("GET /api/stats 200")
        logging.getLogger("uvicorn.error").error("noise")
        entries = ds.log_entries()
        assert any(e["message"] == "hello from bridge" for e in entries)
        assert not any(e["name"].startswith("uvicorn") for e in entries)
    finally:
        ds.remove_log_handler()
    # Removed: new records no longer land in the ring.
    before = len(ds.log_entries())
    logging.getLogger("comulytic.bridge").info("after removal")
    assert len(ds.log_entries()) == before


def test_uptime_positive():
    assert ds.uptime_seconds() >= 0.0