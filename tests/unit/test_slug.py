"""Unit tests for ``slug.generate_slug`` (httpx mocked via MockTransport)."""

import httpx
import pytest

from opencode_discord_bot import slug as slug_module
from opencode_discord_bot.config import config


class _MutableHandler:
    """A mutable MockTransport handler so tests can swap responses per test."""

    def __init__(self):
        self._handler = None

    def set(self, handler):
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self._handler is None:
            return httpx.Response(200, json={})
        return self._handler(request)


@pytest.fixture
def mock_slug_client(monkeypatch):
    """Replace the module-level slug httpx client with one using MockTransport.

    Returns the mutable handler so tests can install their own response.
    """
    handler = _MutableHandler()
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(slug_module, "_slug_client", client)
    return handler


def _set_response(handler, status_code=200, json_body=None, headers=None):
    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body or {}, headers=headers or {})
    handler.set(_h)


def _set_exc(handler, exc):
    def _h(request: httpx.Request) -> httpx.Response:
        raise exc
    handler.set(_h)


def test_normalize_lowercases_and_collapses():
    assert slug_module._normalize("Hello WORLD!") == "hello-world"
    assert slug_module._normalize("a---b") == "a-b"
    assert slug_module._normalize("--ab--") == "ab"


async def test_generate_slug_returns_llm_slug(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 200,
                  {"choices": [{"message": {"content": "my-cool-slug"}}]})
    result = await slug_module.generate_slug("make a cool thing", fallback="fb")
    assert result == "my-cool-slug"


async def test_generate_slug_normalizes_llm_output(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 200,
                  {"choices": [{"message": {"content": "Hello WORLD!"}}]})
    result = await slug_module.generate_slug("x", fallback="fb")
    assert result == "hello-world"


async def test_generate_slug_strips_quotes_around_content(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 200,
                  {"choices": [{"message": {"content": "'quoted-slug'"}}]})
    assert await slug_module.generate_slug("x", fallback="fb") == "quoted-slug"


async def test_generate_slug_empty_prompt_returns_fallback(mock_slug_client):
    assert await slug_module.generate_slug("", fallback="fb") == "fb"
    assert await slug_module.generate_slug("   ", fallback="fb") == "fb"


async def test_generate_slug_empty_key_returns_fallback(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "")
    _set_exc(mock_slug_client, httpx.ConnectError("no key"))
    assert await slug_module.generate_slug("prompt", fallback="fb") == "fb"


async def test_generate_slug_http_500_returns_fallback(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 500, {"error": "oops"})
    assert await slug_module.generate_slug("prompt", fallback="fb") == "fb"


async def test_generate_slug_malformed_json_returns_fallback(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 200, {"unexpected": "shape"})
    assert await slug_module.generate_slug("prompt", fallback="fb") == "fb"


async def test_generate_slug_empty_content_returns_fallback(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 200, {"choices": [{"message": {"content": ""}}]})
    assert await slug_module.generate_slug("prompt", fallback="fb") == "fb"


async def test_generate_slug_whitespace_content_returns_fallback(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_response(mock_slug_client, 200, {"choices": [{"message": {"content": "   "}}]})
    assert await slug_module.generate_slug("prompt", fallback="fb") == "fb"


async def test_generate_slug_never_raises_on_transport_error(mock_slug_client, monkeypatch):
    monkeypatch.setattr(config, "ollama_auth_key", "k")
    _set_exc(mock_slug_client, httpx.ReadTimeout("timed out"))
    assert await slug_module.generate_slug("prompt", fallback="fb") == "fb"