"""Status-polling bridge: relays opencode session progress to Discord.

`poll_until_idle` periodically calls `GET /session/status` for a specific
opencode session, invokes a caller callback on each status change, and
returns when the session becomes idle (the prompt finished).

The previous implementation consumed the global opencode SSE stream
(`GET /event`), but that stream emits v2-native events whose wire format
(``{id, type, data}`` with the event type in the JSON body, not the SSE
``event:`` line, which is always ``"message"``) did not match the v1
``{type, properties}`` shape the old parser expected. The parser silently
skipped every event, so the idle signal was never observed and the relay
loop hung forever — the bot sent the prompt but never relayed the
response. Polling the documented ``/session/status`` endpoint avoids the
entire class of wire-format bugs.

**Fire-and-forget race fix:** ``POST /session/{id}/prompt_async`` returns
``204 No Content`` immediately and forks the prompt work
(``Effect.forkIn(..., { startImmediately: true })`` in the server handler).
There is a window between the 204 response and the forked effect's first
``status.set(sessionID, { type: "busy" })`` during which the session is
absent from the in-memory status map. A missing entry in the status map
*normally* means idle (the server deletes idle sessions from the map), but
in that window it means "the fork hasn't started yet." Without a guard,
``poll_until_idle``'s first poll (which runs with no preceding sleep) would
see the missing entry, treat it as idle, return immediately, and the bot
would fetch ``list_messages`` before any assistant text existed — producing
a "Done (no text output)" reply. The loop now requires the session to be
observed as ``busy`` at least once before a missing/idle entry is treated
as terminal, plus a grace-period backstop so a prompt that errors out
before ever setting busy still terminates (instead of looping until the
overall timeout).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from opencode_discord_bot.opencode_client import OpencodeClient

_log = logging.getLogger("bot.events")

# A session status dict from `GET /session/status`, e.g.
# ``{"type": "busy"}`` or ``{"type": "retry", "attempt": 1, ...}``.
SessionStatus = dict

OnStatus = Callable[[SessionStatus], Awaitable[None]]

# Grace period for the forked prompt effect to set the session "busy" for the
# first time. If the session is never observed as busy within this window
# (measured from the start of polling), a missing/idle status is treated as
# terminal anyway — the prompt likely errored out before its first
# `status.set(..., busy)`, and looping until the overall `timeout` (typically
# 1800s) would needlessly block the caller. Long enough to absorb fork
# scheduling + HTTP round-trip latency, short enough to fail fast on a
# genuinely-dead prompt.
_BUSY_GRACE_SECONDS = 30.0


async def poll_until_idle(
    client: OpencodeClient,
    session_id: str,
    on_status: OnStatus,
    *,
    interval: float = 2.0,
    timeout: float | None = None,
) -> SessionStatus:
    """Poll ``GET /session/status`` for ``session_id`` until it is idle.

    The loop:
    1. Fetches the session-status map (``{sessionID: SessionStatus}``).
    2. Extracts the entry for ``session_id``; a missing entry is treated as
       ``{"type": "idle"}`` — but is only terminal once the session has been
       observed as ``busy`` at least once (or the ``_BUSY_GRACE_SECONDS``
       grace period has elapsed). Before either of those, a missing entry
       means the forked ``prompt_async`` work hasn't set busy yet, so the
       loop keeps polling.
    3. On any status *change*, invokes ``on_status(status)`` so the caller
       can render progress. Identical consecutive statuses are skipped to
       avoid spamming the Discord message-edit rate limit.
    4. Returns the final status once ``type == "idle"`` is terminal (see #2).

    The first fetch is preceded by a short sleep equal to `interval` so the
    forked effect has a chance to set busy before the first observation
    (reducing, though not eliminating, the window in which the grace-period
    backstop is what terminates the loop).

    If ``timeout`` is set and elapses before idle, raises
    ``asyncio.TimeoutError`` so the caller can fetch whatever partial
    output exists and reply with it rather than hanging forever.

    Caller cancellation (``asyncio.CancelledError``) propagates cleanly,
    which ``OpencodeBot.oc_abort`` relies on to stop polling after an abort.
    """
    loop = asyncio.get_event_loop()
    deadline: float | None = loop.time() + timeout if timeout is not None else None
    start = loop.time()
    last_status: SessionStatus | None = None
    saw_busy = False
    while True:
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"session {session_id} did not become idle within {timeout}s"
                )
            sleep_for = min(interval, remaining)
        else:
            sleep_for = interval

        # Sleep BEFORE the fetch (including the first iteration) so the
        # forked prompt_async effect has a chance to run `status.set(...,
        # busy)` before we first observe the status map. This reduces the
        # window in which the grace-period backstop is the only thing
        # keeping the loop alive.
        await asyncio.sleep(sleep_for)

        try:
            status_map = await client.get_session_status()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — transient HTTP errors shouldn't kill the poll
            _log.warning("session status fetch failed: %r — retrying", exc)
            status_map = {}

        status = status_map.get(session_id) or {"type": "idle"}

        if status.get("type") == "busy":
            saw_busy = True

        if status != last_status:
            last_status = status
            try:
                await on_status(status)
            except (
                Exception
            ):  # noqa: BLE001 — a progress-render error must not kill the poll
                _log.warning("on_status callback raised", exc_info=True)

        if status.get("type") == "idle":
            # Terminal only once the session has been observed busy (the
            # prompt actually started), or once the grace period has
            # elapsed (the prompt likely errored before setting busy —
            # fail fast instead of looping until the overall timeout).
            if saw_busy or (loop.time() - start) >= _BUSY_GRACE_SECONDS:
                if not saw_busy:
                    _log.warning(
                        "session %s never observed busy within %.1fs grace "
                        "period; treating as idle",
                        session_id,
                        _BUSY_GRACE_SECONDS,
                    )
                return status
            # else: fork hasn't set busy yet; keep polling
