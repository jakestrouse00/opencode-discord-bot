"""Unit tests for the read-only session monitor (`monitor.py`).

All network boundaries are fakes: a `ScriptedOpencodeClient` for the
opencode REST surface, a `FakeMonitorBot` duck-typed stand-in for
`OpencodeBot` (router / bridge_router / fetch_channel), and a plain
record list for the notification channel. No gateway, no real Discord,
no real opencode server — matching the suite-wide convention in
`tests/fakes.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from opencode_discord_bot import monitor
from opencode_discord_bot.text_utils import permission_block, question_block
from tests.fakes import (
    ScriptedOpencodeClient,
    assistant_message,
    permission_request,
    question_request,
)


# ---------------------------------------------------------------------------
# Stand-ins for the monitor's narrow bot + channel surface
# ---------------------------------------------------------------------------


class FakeMonitorChannel:
    """Records every send call (content + embed)."""

    def __init__(self):
        self.sent: list[tuple[str | None, object]] = []

    async def send(self, content=None, *, embed=None, **kw):
        self.sent.append((content, embed))
        return None


class FakeMonitorBot:
    """Duck-typed stand-in for OpencodeBot's monitor-facing surface:
    `.client`, `.router`, `.bridge_router`, `.fetch_channel(id)`, and the
    optional `.get_channel(id)` gateway cache lookup."""

    def __init__(self, client, channel, *, router=None, bridge_router=None):
        self.client = client
        self.router = router
        self.bridge_router = bridge_router
        self._channel = channel

    def get_channel(self, channel_id):
        return None

    async def fetch_channel(self, channel_id):
        return self._channel


class FakeRouter:
    """Minimal SessionRouter stand-in — only the `_map` values are read."""

    def __init__(self, bindings: dict[str, str]):
        self._map = dict(bindings)


# ---------------------------------------------------------------------------
# Embed builders (pure functions — no loop, no bot)
# ---------------------------------------------------------------------------


class TestEmbedBuilders:
    def test_question_embed_includes_header_options_footer(self):
        req = question_request(rid="q-1", sid="sid-1")
        embed = monitor.question_embed("My Title", "sid-1", req)
        assert "My Title" in embed.title
        assert embed.footer.text == "session sid-1"
        desc = embed.description
        assert "Pick a file" in desc
        assert "main.py" in desc
        assert "utils.py" in desc
        assert question_block(req["questions"][0]) in desc

    def test_question_embed_empty_request(self):
        embed = monitor.question_embed("T", "sid-2", {"id": "q", "questions": []})
        assert "no questions in request" in embed.description

    def test_permission_embed_includes_name_patterns_footer(self):
        req = permission_request(rid="p-1", sid="sid-3", permission="bash")
        embed = monitor.permission_embed("Build", "sid-3", req)
        assert "Build" in embed.title
        assert "Permission: bash" in embed.description
        assert "`src/main.py`" in embed.description
        assert embed.footer.text == "session sid-3"

    def test_permission_embed_doom_loop_warning(self):
        req = permission_request(rid="p-2", sid="sid-4", permission="doom_loop")
        embed = monitor.permission_embed("T", "sid-4", req)
        assert "stuck in a loop" in embed.description
        assert permission_block(req) in embed.description

    def test_completion_embed_snippet_truncated(self):
        long_text = "x" * 1000
        embed = monitor.completion_embed("T", "sid-5", long_text)
        assert len(embed.description) <= monitor._SNIPPET_MAX
        assert embed.description.endswith("…")

    def test_completion_embed_snippet_short_untouched(self):
        embed = monitor.completion_embed("T", "sid-6", "done!")
        assert embed.description == "done!"

    def test_completion_embed_empty_snippet(self):
        embed = monitor.completion_embed("T", "sid-7", "")
        assert "no text output" in embed.description


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def _run_cycles(bot, cycles: int, *, exclude_none_routers: bool = True) -> None:
    """Drive `run_monitor` for exactly N poll cycles, then cancel it.

    Uses a side-effect hook on get_session_status to cancel the monitor
    task on the Nth cycle so the loop exits deterministically (the sleep
    between cycles is also real asyncio sleep; with the default 10s
    interval that would stall tests, so `config.monitor_poll_interval_seconds`
    is monkeypatched to ~0).
    """

    async def runner():
        await monitor.run_monitor(bot)

    task = asyncio.create_task(runner())
    state = {"n": 0}
    client = bot.client

    def on_status(_queue):
        state["n"] += 1
        if state["n"] >= cycles:
            task.cancel()

    client.on_call("get_session_status", on_status)
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.CancelledError:
        pass
    client._side_effects.clear()


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    from opencode_discord_bot.config import config

    monkeypatch.setattr(config, "monitor_poll_interval_seconds", 0.01)
    yield


@pytest.fixture(autouse=True)
def monitor_config(monkeypatch):
    from opencode_discord_bot.config import config

    monkeypatch.setattr(config, "monitor_channel_id", 1544715093491847249)
    monkeypatch.setattr(config, "monitor_user_id", 4242)
    yield


class TestPollLoop:
    async def test_question_event_posts_embed(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        q = question_request(rid="q-1", sid="sess-a")
        client.script("list_questions", [q], [])
        client.script("get_session", {"id": "sess-a", "title": "Desktop task"})
        await _run_cycles(bot, cycles=3)
        embeds = [e for _, e in channel.sent if e is not None]
        assert len(embeds) == 1
        assert "Desktop task" in embeds[0].title
        assert "Pick a file" in embeds[0].description

    async def test_question_mention_prefix(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        client.script("list_questions", [question_request(rid="q-9", sid="s")], [])
        await _run_cycles(bot, cycles=2)
        assert channel.sent, "expected at least one post"
        content, _ = channel.sent[0]
        assert content == "<@4242>"

    async def test_permission_event_posts_embed(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        p = permission_request(rid="p-1", sid="sess-b", permission="bash")
        client.script("list_permissions", [p], [])
        client.script("get_session", {"id": "sess-b", "title": "Deploy"})
        await _run_cycles(bot, cycles=3)
        embeds = [e for _, e in channel.sent if e is not None]
        assert len(embeds) == 1
        assert "Permission needed" in embeds[0].title
        assert "bash" in embeds[0].description

    async def test_busy_to_idle_posts_completion_with_snippet(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        # Cycle 1: session busy. Later cycles: gone from the map (idle).
        client.script(
            "get_session_status", {"sess-c": {"type": "busy"}}, {}, {}, {}
        )
        client.script("get_session", {"id": "sess-c", "title": "Long task"})
        client.script(
            "list_messages", [assistant_message("All done, files written.")]
        )
        await _run_cycles(bot, cycles=4)
        embeds = [e for _, e in channel.sent if e is not None]
        assert len(embeds) == 1
        assert "completed" in embeds[0].title.lower()
        assert "Long task" in embeds[0].title
        assert "All done, files written." in embeds[0].description

    async def test_idle_at_start_no_completion(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        # Never busy, never completes — no posts expected.
        await _run_cycles(bot, cycles=3)
        assert channel.sent == []

    async def test_router_bound_sessions_excluded(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        router = FakeRouter({"100": "sess-main"})
        bot = FakeMonitorBot(client, channel, router=router)
        q = question_request(rid="q-1", sid="sess-main")
        client.script("list_questions", [q], [])
        client.script(
            "get_session_status",
            {"sess-main": {"type": "busy"}},
            {},
            {},
        )
        client.script("list_permissions", [permission_request(rid="p-1", sid="sess-main")], [])
        await _run_cycles(bot, cycles=4)
        # No question embed, no permission embed, no completion embed —
        # the session is bound to a Discord channel already.
        assert channel.sent == []

    async def test_bridge_router_bound_sessions_excluded(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bridge_router = FakeRouter({"200": "sess-bridge"})
        bot = FakeMonitorBot(client, channel, bridge_router=bridge_router)
        q = question_request(rid="q-1", sid="sess-bridge")
        client.script("list_questions", [q], [])
        await _run_cycles(bot, cycles=2)
        assert channel.sent == []

    async def test_seen_request_ids_not_reposted(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        q = question_request(rid="q-1", sid="sess-d")
        # Cycle 1 sees the request; cycles 2-3 see it again (still pending).
        client.script("list_questions", [q], [q], [q])
        await _run_cycles(bot, cycles=4)
        embeds = [e for _, e in channel.sent if e is not None]
        assert len(embeds) == 1

    async def test_list_questions_error_swallowed(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        err = RuntimeError("boom")
        # Cycle 1 raises; a later cycle surfaces a real question.
        client.script_exc("list_questions", err)
        q = question_request(rid="q-1", sid="sess-e")
        client.script("list_questions", [q], [])
        await _run_cycles(bot, cycles=3)
        embeds = [e for _, e in channel.sent if e is not None]
        assert len(embeds) == 1

    async def test_retry_status_tracked_to_completion(self):
        client = ScriptedOpencodeClient()
        channel = FakeMonitorChannel()
        bot = FakeMonitorBot(client, channel)
        # "retry" counts as running; leaving the map completes it.
        client.script(
            "get_session_status", {"sess-r": {"type": "retry", "attempt": 1}}, {}, {}
        )
        client.script("get_session", {"id": "sess-r", "title": "Flaky"})
        client.script("list_messages", [assistant_message("Recovered.")])
        await _run_cycles(bot, cycles=3)
        embeds = [e for _, e in channel.sent if e is not None]
        assert len(embeds) == 1
        assert "Recovered." in embeds[0].description