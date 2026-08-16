"""Unit tests for ``discord_rest.DiscordRest`` (httpx via MockTransport)."""

import json

import httpx
import pytest

from opencode_discord_bot.discord_rest import DiscordRest, DiscordRestError


def _make(transport, token="test-token"):
    rest = DiscordRest(token, base_url="https://discord.test/api/v10")
    # Replace the real httpx client with one using the mock transport.
    rest._client = httpx.AsyncClient(
        base_url="https://discord.test/api/v10",
        headers={"Authorization": f"Bot {token}"},
        transport=transport,
    )
    return rest


def test_empty_token_raises():
    with pytest.raises(DiscordRestError, match="empty"):
        DiscordRest("")


@pytest.mark.parametrize(
    "method,expected_path,expected_method,call_args",
    [
        ("create_text_channel", "/api/v10/guilds/1/channels", "POST",
         lambda r: r.create_text_channel(1, "test", parent_id=5, topic="t")),
        ("edit_channel", "/api/v10/channels/9", "PATCH",
         lambda r: r.edit_channel(9, "new")),
        ("create_message", "/api/v10/channels/9/messages", "POST",
         lambda r: r.create_message(9, "hi")),
    ],
)
async def test_post_endpoints_hit_correct_paths(method, expected_path, expected_method, call_args):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        return httpx.Response(200, json={"id": 42})

    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        await call_args(rest)
        assert captured["path"] == expected_path
        assert captured["method"] == expected_method
    finally:
        await rest.aclose()


async def test_create_text_channel_returns_id():
    def handler(request):
        return httpx.Response(200, json={"id": 12345, "name": "test"})
    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        result = await rest.create_text_channel(1, "test")
        assert result["id"] == 12345
    finally:
        await rest.aclose()


async def test_create_message_returns_id():
    def handler(request):
        return httpx.Response(200, json={"id": 999})
    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        result = await rest.create_message(1, "hi")
        assert result["id"] == 999
    finally:
        await rest.aclose()


async def test_list_messages_returns_list():
    def handler(request):
        return httpx.Response(200, json=[{"id": "1"}, {"id": "2"}])
    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        msgs = await rest.list_messages(1, after=100, limit=50)
        assert len(msgs) == 2
    finally:
        await rest.aclose()


async def test_non_2xx_raises_discordresterror():
    def handler(request):
        return httpx.Response(403, text="forbidden")
    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        with pytest.raises(DiscordRestError, match="403"):
            await rest.create_message(1, "x")
    finally:
        await rest.aclose()


async def test_204_returns_empty_dict():
    def handler(request):
        return httpx.Response(204)
    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        result = await rest.create_message(1, "x")
        assert result == {}
    finally:
        await rest.aclose()


async def test_429_retries_once_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"id": 1})

    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        result = await rest.create_message(1, "x")
        assert calls["n"] == 2
        assert result["id"] == 1
    finally:
        await rest.aclose()


async def test_429_twice_raises():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0.01"})
    transport = httpx.MockTransport(handler)
    rest = _make(transport)
    try:
        with pytest.raises(DiscordRestError, match="429"):
            await rest.create_message(1, "x")
    finally:
        await rest.aclose()