"""In-process shared state for the ops dashboard (stats + controls).

The dashboard runs as an in-process task alongside the main bot and the
Comulytic bridge (both share one event loop), so module-level state is
race-free and needs no lock — the same pattern as ``bridge_state.py``.

Two directions of flow:

* **Bridge -> dashboard (publish):** the bridge records poll results,
  per-recording outcomes, and registers its in-memory seen-set reference
  so the dashboard can report ``seen_count`` without touching the
  persistence file.
* **Dashboard -> bridge (control):** the dashboard flips flags
  (skip-transcription, pause) and sets a *pending action* for seen-set
  mutations. Seen-set writes are owned by the bridge task (AGENTS.md:
  the bridge OWNS writes to ``.comulytic-seen.json``), so the dashboard
  never mutates the set directly — it queues an action the bridge applies
  at the top of its next poll cycle.

All control state is deliberately EPHEMERAL: skip/pause flags and pending
actions reset to their defaults on process restart (nothing is persisted to
the volume). Counters, history, and the log ring are likewise in-memory
only.

This module intentionally has no dependency on the rest of the bridge or
dashboard import surface so importing it into ``bridge.py`` and
``commands.py`` stays cheap (mirrors the ``bridge_state.py`` pattern).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

# ---------------------------------------------------------------------------
# Control flags (dashboard -> bridge). All default OFF and are NOT persisted
# — a restart resets them (explicit user decision: an accidentally-left-on
# skip toggle must never silently drop recordings after a redeploy).
# ---------------------------------------------------------------------------

_skip_transcription: bool = False
_paused: bool = False

# Pending seen-set action for the bridge to apply on its next poll cycle:
# None | "mark_all_seen" | "clear_seen". Seen-set writes are bridge-owned,
# so the dashboard only queues the intent here.
_pending_action: str | None = None


def is_skipping_transcription() -> bool:
    """True iff new audio-delivered recordings should be marked seen but
    NOT transcribed/routed (the accidental-recording kill switch)."""
    return _skip_transcription


def set_skip_transcription(value: bool) -> None:
    global _skip_transcription
    _skip_transcription = bool(value)


def is_paused() -> bool:
    """True iff the Comulytic poll loop is paused (poll cycles are skipped
    entirely — nothing is marked seen while paused, so the backlog
    processes on resume)."""
    return _paused


def set_paused(value: bool) -> None:
    global _paused
    _paused = bool(value)


def request_action(action: str) -> None:
    """Queue a seen-set mutation for the bridge ("mark_all_seen" or
    "clear_seen"). Overwrites any pending action; the bridge consumes it
    via ``take_pending_action`` at the top of its next poll cycle."""
    global _pending_action
    _pending_action = action


def take_pending_action() -> str | None:
    """Return and clear the queued seen-set action (bridge side)."""
    global _pending_action
    action = _pending_action
    _pending_action = None
    return action


def pending_action() -> str | None:
    """Read the queued seen-set action without clearing it (for stats)."""
    return _pending_action


# ---------------------------------------------------------------------------
# In-flight processing (bridge -> dashboard). The bridge registers the
# per-recording task it is currently awaiting so the dashboard's abort
# button can cancel JUST that recording — not the bridge task itself
# (cancelling the bridge task would kill polling entirely).
# ---------------------------------------------------------------------------

_in_flight_task: asyncio.Task[Any] | None = None
_in_flight_note_id: str | None = None


def set_in_flight(task: asyncio.Task[Any], note_id: str) -> None:
    global _in_flight_task, _in_flight_note_id
    _in_flight_task = task
    _in_flight_note_id = note_id


def clear_in_flight() -> None:
    global _in_flight_task, _in_flight_note_id
    _in_flight_task = None
    _in_flight_note_id = None


def in_flight_note_id() -> str | None:
    return _in_flight_note_id


def abort_in_flight() -> bool:
    """Cancel the in-flight recording task, if any. Returns True when a
    cancellation was requested. The recording is already in the seen-set,
    so it will NOT reprocess on the next cycle."""
    task = _in_flight_task
    if task is None or task.done():
        return False
    task.cancel()
    return True


# ---------------------------------------------------------------------------
# Metrics (bridge -> dashboard).
# ---------------------------------------------------------------------------

_processed: int = 0
_skipped: int = 0
_failed: int = 0
_last_poll_at: float | None = None
_last_poll_status: str = ""
# Per-recording history entries: {"note_id", "status", "seconds", "at"}.
_recent: deque[dict[str, Any]] = deque(maxlen=50)
# Read-only reference to the bridge's in-memory seen-set (registered once
# at bridge start; used for the seen_count stat only — never mutated here).
_seen_ref: set[str] | None = None
_started_at: float = time.monotonic()


def record_processed() -> None:
    global _processed
    _processed += 1


def record_skipped(count: int = 1) -> None:
    global _skipped
    _skipped += count


def record_failed() -> None:
    global _failed
    _failed += 1


def set_poll_result(status: str) -> None:
    global _last_poll_at, _last_poll_status
    _last_poll_at = time.time()
    _last_poll_status = status


def add_history(note_id: str, status: str, seconds: float) -> None:
    _recent.append(
        {
            "note_id": note_id,
            "status": status,
            "seconds": round(seconds, 1),
            "at": time.time(),
        }
    )


def register_seen(seen: set[str]) -> None:
    """Register the bridge's live in-memory seen-set for read-only stats."""
    global _seen_ref
    _seen_ref = seen


def reset() -> None:
    """Reset all state to defaults (test helper; also the honest answer to
    'what does a restart do' — everything resets)."""
    global _skip_transcription, _paused, _pending_action
    global _in_flight_task, _in_flight_note_id
    global _processed, _skipped, _failed, _last_poll_at, _last_poll_status
    global _seen_ref, _started_at
    _skip_transcription = False
    _paused = False
    _pending_action = None
    _in_flight_task = None
    _in_flight_note_id = None
    _processed = 0
    _skipped = 0
    _failed = 0
    _last_poll_at = None
    _last_poll_status = ""
    _recent.clear()
    _seen_ref = None
    _started_at = time.monotonic()


def snapshot() -> dict[str, Any]:
    """A JSON-able snapshot of the current state (the /api/stats payload's
    bridge block)."""
    return {
        "skip_transcription": _skip_transcription,
        "paused": _paused,
        "pending_action": _pending_action,
        "processed": _processed,
        "skipped": _skipped,
        "failed": _failed,
        "last_poll_at": _last_poll_at,
        "last_poll_status": _last_poll_status,
        "in_flight_note_id": _in_flight_note_id,
        "seen_count": len(_seen_ref) if _seen_ref is not None else None,
        "recent": list(_recent),
    }


# ---------------------------------------------------------------------------
# Log ring. A logging.Handler that keeps the last 500 records in a deque
# so the dashboard can render recent logs without `flyctl logs`. Uvicorn's
# own access/error loggers are excluded (pure noise for this surface).
# ---------------------------------------------------------------------------

_log_ring: deque[dict[str, Any]] = deque(maxlen=500)


class RingLogHandler(logging.Handler):
    """Append formatted log records to the in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if name.startswith("uvicorn"):
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — formatting must never raise
            message = record.msg if isinstance(record.msg, str) else repr(record.msg)
        _log_ring.append(
            {
                "ts": record.created,
                "level": record.levelname,
                "name": name,
                "message": message,
            }
        )


_ring_handler: RingLogHandler | None = None


def install_log_handler() -> None:
    """Attach the ring handler to the root logger (idempotent)."""
    global _ring_handler
    if _ring_handler is None:
        _ring_handler = RingLogHandler(level=logging.INFO)
        logging.getLogger().addHandler(_ring_handler)


def remove_log_handler() -> None:
    """Detach the ring handler from the root logger (idempotent)."""
    global _ring_handler
    if _ring_handler is not None:
        logging.getLogger().removeHandler(_ring_handler)
        _ring_handler = None


def log_entries() -> list[dict[str, Any]]:
    """A copy of the current log ring (oldest first)."""
    return list(_log_ring)


def uptime_seconds() -> float:
    """Seconds since this module was first imported (process uptime)."""
    return time.monotonic() - _started_at