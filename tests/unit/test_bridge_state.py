"""Unit tests for the in-process bridge-state tracker."""

from opencode_discord_bot.bridge_state import mark_active, clear_active, is_active, _active_sids


def test_mark_active_then_is_active():
    mark_active("sid-1")
    assert is_active("sid-1")


def test_clear_active_removes():
    mark_active("sid-1")
    clear_active("sid-1")
    assert not is_active("sid-1")


def test_clear_active_missing_is_noop():
    clear_active("never-was")  # no error
    assert not is_active("never-was")


def test_mark_active_idempotent():
    mark_active("sid-1")
    mark_active("sid-1")
    assert is_active("sid-1")
    assert len(_active_sids & {"sid-1"}) == 1


def test_isolates_sids():
    mark_active("sid-a")
    mark_active("sid-b")
    assert is_active("sid-a")
    assert is_active("sid-b")
    clear_active("sid-a")
    assert not is_active("sid-a")
    assert is_active("sid-b")


def test_empty_sid_ignored():
    mark_active("")
    assert "" not in _active_sids