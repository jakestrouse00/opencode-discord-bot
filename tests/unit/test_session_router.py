"""Unit tests for ``session_router.SessionRouter`` (real file at tmp_path)."""

import json

import pytest

from opencode_discord_bot.session_router import SessionRouter


@pytest.fixture
def router(tmp_router_path):
    return SessionRouter(tmp_router_path)


def test_empty_file_returns_none(router):
    assert router.current(123) is None


async def test_bind_then_current(router):
    await router.bind(123, "sid-1")
    assert router.current(123) == "sid-1"


async def test_reset_removes_binding(router):
    await router.bind(123, "sid-1")
    await router.reset(123)
    assert router.current(123) is None


async def test_reset_on_unbound_is_noop(router):
    await router.reset(999)  # no error


async def test_persistence_round_trip(tmp_router_path):
    # Bind in one router, save, construct a fresh router on the same file.
    r1 = SessionRouter(tmp_router_path)
    await r1.bind(555, "sid-persist")
    r2 = SessionRouter(tmp_router_path)
    assert r2.current(555) == "sid-persist"


async def test_corrupt_file_recovers(tmp_router_path):
    tmp_router_path.write_text("{not valid json")
    r = SessionRouter(tmp_router_path)
    assert r.current(1) is None  # empty map, not a crash
    await r.bind(7, "sid")
    assert r.current(7) == "sid"


async def test_get_or_create_creates_session(router):
    from tests.fakes import ScriptedOpencodeClient

    fake = ScriptedOpencodeClient()
    fake.script("create_session", {"id": "new-sid-99"})
    sid = await router.get_or_create(42, fake)
    assert sid == "new-sid-99"
    assert router.current(42) == "new-sid-99"


async def test_get_or_create_returns_existing(router):
    from tests.fakes import ScriptedOpencodeClient

    fake = ScriptedOpencodeClient()
    await router.bind(11, "existing-sid")
    sid = await router.get_or_create(11, fake)
    assert sid == "existing-sid"
    # create_session should NOT have been called.
    assert not any(c[0] == "create_session" for c in fake.calls)