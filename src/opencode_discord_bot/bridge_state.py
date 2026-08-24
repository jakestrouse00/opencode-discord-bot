"""In-process shared state for which opencode sessions the Comulytic bridge
is currently driving.

The bridge runs as an in-process ``asyncio`` task alongside the main bot
(the auto-spawn path, ``OpencodeBot.on_connect`` → ``run_bridge``). Both
share one event loop, so a module-level ``set`` is race-free and needs no
lock. The main bot's ``on_message`` consults ``is_active`` to decide
whether to yield to the bridge's REST reply-poller (which bundles
clarifying-question answers into a single ``reply_question`` call) rather
than re-dispatching each user message as a fresh ``send_prompt_async``
follow-up — the dual-consumer race that produced "Session is busy"
errors and duplicate final-response messages.

The standalone ``comulytic-bridge`` console script runs in a SEPARATE
process and is NOT visible here; for that deployment a file-based/
heartbeat liveness signal would be needed (see AGENTS.md
"Comulytic bridge auto-spawn"). This module intentionally has no
dependency on the rest of the bridge import surface so importing it into
``commands.py`` stays cheap (mirrors the ``_BRIDGE_SESSIONS_FILE`` string
constant pattern).
"""

from __future__ import annotations

_active_sids: set[str] = set()


def mark_active(sid: str) -> None:
    """Record that the bridge is currently driving ``sid`` (clarifying
    questions in flight, prompt mid-turn, etc.).

    Called by ``route_to_assistant`` right after ``create_session``
    returns the id. Safe to call repeatedly for the same sid (idempotent
    set add). Must be balanced by ``clear_active`` once the drive ends.
    """
    if not sid:
        return
    _active_sids.add(sid)


def clear_active(sid: str) -> None:
    """Mark ``sid`` as no longer being driven by the bridge.

    Called in the ``finally`` wrapping ``route_to_assistant``'s drive
    section, so it runs on success, timeout, ``OpencodeError``, and
    cancellation (``OpencodeBot.close`` cancels the bridge task).
    Discarding a missing sid is a no-op, so a double-clear is harmless.
    """
    _active_sids.discard(sid)


def is_active(sid: str) -> bool:
    """True iff the bridge is currently mid-turn on ``sid``.

    The main bot's ``on_message`` checks this BEFORE its own
    ``_active_drives`` membership test so it can yield silently (the
    bridge's REST poller will consume the reply) instead of racing.
    """
    return sid in _active_sids