"""Unit tests for ``events.poll_until_idle`` (uses ``ScriptedOpencodeClient``)."""

import asyncio

import pytest

from opencode_discord_bot.events import poll_until_idle, _BUSY_GRACE_SECONDS
from tests.fakes import ScriptedOpencodeClient


async def test_busy_then_idle_returns_idle():
    client = ScriptedOpencodeClient()
    # The first poll sleeps `interval` before fetching, so the scripted
    # sequence matches: poll1=busy, poll2=idle.
    client.script("get_session_status",
                  {"sid-1": {"type": "busy"}},
                  {"sid-1": {"type": "idle"}})
    on_status_calls = []

    async def on_status(status):
        on_status_calls.append(status)

    result = await poll_until_idle(client, "sid-1", on_status, interval=0.0, timeout=10)
    assert result["type"] == "idle"
    # on_status called once for busy, once for idle.
    assert len(on_status_calls) == 2
    assert on_status_calls[0]["type"] == "busy"
    assert on_status_calls[1]["type"] == "idle"


async def test_never_busy_grace_elapsed_returns_idle():
    client = ScriptedOpencodeClient()
    # Always idle from the start — the grace period backstop should terminate.
    client.script("get_session_status",
                  {"sid-1": {"type": "idle"}},
                  {"sid-1": {"type": "idle"}})
    # Use a tiny grace by patching the module constant.
    import opencode_discord_bot.events as events_mod
    orig = events_mod._BUSY_GRACE_SECONDS
    events_mod._BUSY_GRACE_SECONDS = 0.0
    try:
        result = await poll_until_idle(client, "sid-1", lambda s: asyncio.sleep(0),
                                        interval=0.0, timeout=5)
        assert result["type"] == "idle"
    finally:
        events_mod._BUSY_GRACE_SECONDS = orig


async def test_never_busy_no_grace_keeps_polling():
    client = ScriptedOpencodeClient()
    client.script("get_session_status",
                  {"sid-1": {"type": "idle"}},
                  {"sid-1": {"type": "idle"}})
    # With a real grace period, the first idle poll should NOT terminate —
    # but the second poll will (grace will have elapsed). Use a short timeout
    # so the test can't hang if the backstop fails.
    import opencode_discord_bot.events as events_mod
    # Ensure the grace is longer than the test will wait.
    events_mod._BUSY_GRACE_SECONDS = 1000.0
    try:
        with pytest.raises(asyncio.TimeoutError):
            await poll_until_idle(client, "sid-1", lambda s: asyncio.sleep(0),
                                  interval=0.01, timeout=0.1)
    finally:
        events_mod._BUSY_GRACE_SECONDS = 30.0


async def test_timeout_raises_timeouterror():
    client = ScriptedOpencodeClient()
    # Always busy — never goes idle. Override the default so the queue
    # (which empties after 3) keeps returning busy.
    client.script("get_session_status",
                  {"sid-1": {"type": "busy"}},
                  {"sid-1": {"type": "busy"}},
                  {"sid-1": {"type": "busy"}})

    def _busy_default(method):
        if method == "get_session_status":
            return {"sid-1": {"type": "busy"}}
        return None
    client._default_for = _busy_default
    with pytest.raises(asyncio.TimeoutError):
        await poll_until_idle(client, "sid-1", lambda s: asyncio.sleep(0),
                              interval=0.0, timeout=0.1)


async def test_on_status_exception_does_not_kill_loop():
    client = ScriptedOpencodeClient()
    client.script("get_session_status",
                  {"sid-1": {"type": "busy"}},
                  {"sid-1": {"type": "idle"}})

    call_count = {"n": 0}

    async def on_status(status):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("on_status blew up")

    result = await poll_until_idle(client, "sid-1", on_status, interval=0.0, timeout=10)
    # The exception was swallowed; the loop continued to idle.
    assert result["type"] == "idle"


async def test_status_fetch_exception_swallowed():
    client = ScriptedOpencodeClient()
    # First call raises, subsequent calls return busy then idle.
    client.script_exc("get_session_status",
                      RuntimeError("transient"),
                      {"sid-1": {"type": "busy"}},
                      {"sid-1": {"type": "idle"}})
    result = await poll_until_idle(client, "sid-1", lambda s: asyncio.sleep(0),
                                    interval=0.0, timeout=10)
    assert result["type"] == "idle"


async def test_identical_status_does_not_re_invoke_on_status():
    client = ScriptedOpencodeClient()
    client.script("get_session_status",
                  {"sid-1": {"type": "busy"}},
                  {"sid-1": {"type": "busy"}},  # unchanged
                  {"sid-1": {"type": "idle"}})
    calls = []

    async def on_status(status):
        calls.append(status)

    await poll_until_idle(client, "sid-1", on_status, interval=0.0, timeout=10)
    # busy (call 1), busy-unchanged (skipped), idle (call 2).
    assert len(calls) == 2