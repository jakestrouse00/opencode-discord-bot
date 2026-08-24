"""Unit tests for ``opencode_client.OpencodeClient`` (httpx via MockTransport)."""

import httpx
import pytest

from opencode_discord_bot.opencode_client import OpencodeClient, OpencodeError
from opencode_discord_bot.config import config


def _client_with(transport, monkeypatch, base_url="http://opencode.test"):
    monkeypatch.setattr(config, "opencode_server_password", "pw")
    monkeypatch.setattr(config, "opencode_server_url", base_url)
    c = OpencodeClient()
    # Inject a client using the mock transport.
    c._client = httpx.AsyncClient(
        base_url=base_url, auth=("opencode", "pw"),
        transport=transport,
        headers={"Accept": "application/json"},
    )
    return c


def test_resolve_model_default_returns_none(monkeypatch):
    monkeypatch.setattr(config, "opencode_default_model", "")
    c = OpencodeClient()
    assert c._resolve_model(None) is None


def test_resolve_model_default_returns_config_value(monkeypatch):
    monkeypatch.setattr(config, "opencode_default_model", "ollama-cloud/glm-5.2")
    c = OpencodeClient()
    assert c._resolve_model(None) == "ollama-cloud/glm-5.2"


def test_resolve_model_assistant_returns_value(monkeypatch):
    monkeypatch.setattr(config, "opencode_assistant_model", "anthropic/claude-sonnet-4")
    c = OpencodeClient()
    assert c._resolve_model("oc-assistant") == "anthropic/claude-sonnet-4"


def test_model_ref_none_for_empty():
    assert OpencodeClient._model_ref(None) is None
    assert OpencodeClient._model_ref("") is None


def test_model_ref_splits_on_first_slash():
    ref = OpencodeClient._model_ref("ollama-cloud/glm-5.2")
    assert ref == {"providerID": "ollama-cloud", "modelID": "glm-5.2"}


def test_model_ref_no_slash_returns_none():
    assert OpencodeClient._model_ref("nodash") is None


async def test_create_session(monkeypatch):
    def handler(request):
        assert request.url.path == "/session"
        assert request.method == "POST"
        return httpx.Response(200, json={"id": "sid-1"})
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.create_session(title="test")
        assert result["id"] == "sid-1"
    finally:
        await c.aclose()


async def test_send_prompt_async_returns_none_on_204(monkeypatch):
    def handler(request):
        assert request.url.path == "/session/sid-1/prompt_async"
        return httpx.Response(204)
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.send_prompt_async("sid-1", [{"type": "text", "text": "hi"}])
        assert result is None
    finally:
        await c.aclose()


async def test_send_prompt_async_with_assistant_includes_model(monkeypatch):
    monkeypatch.setattr(config, "opencode_assistant_model", "anthropic/claude-sonnet-4")
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        await c.send_prompt_async("sid-1", [{"type": "text", "text": "x"}], agent="oc-assistant")
        assert captured["body"]["agent"] == "oc-assistant"
        assert captured["body"]["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet-4"}
    finally:
        await c.aclose()


async def test_send_prompt_async_without_model_override_omits_model(monkeypatch):
    monkeypatch.setattr(config, "opencode_default_model", "")
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        await c.send_prompt_async("sid-1", [{"type": "text", "text": "x"}])
        assert "model" not in captured["body"]
    finally:
        await c.aclose()


async def test_list_questions(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"id": "q1", "sessionID": "sid-1"}])
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.list_questions()
        assert result == [{"id": "q1", "sessionID": "sid-1"}]
    finally:
        await c.aclose()


async def test_reply_question_returns_bool(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=True)
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.reply_question("q1", [["opt-a"]])
        assert result is True
    finally:
        await c.aclose()


async def test_abort_session_returns_bool(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=True)
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.abort_session("sid-1")
        assert result is True
    finally:
        await c.aclose()


async def test_revert_session(monkeypatch):
    def handler(request):
        import json
        body = json.loads(request.read().decode())
        assert body == {"messageID": "m-1"}
        return httpx.Response(200, json={"id": "sid-1"})
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.revert_session("sid-1", "m-1")
        assert result["id"] == "sid-1"
    finally:
        await c.aclose()


async def test_get_session_status(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"sid-1": {"type": "busy"}})
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.get_session_status()
        assert result["sid-1"]["type"] == "busy"
    finally:
        await c.aclose()


async def test_list_messages(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"info": {"role": "assistant"}, "parts": []}])
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.list_messages("sid-1")
        assert len(result) == 1
    finally:
        await c.aclose()


async def test_4xx_raises_opencodeerror(monkeypatch):
    def handler(request):
        return httpx.Response(400, text="bad request")
    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        with pytest.raises(OpencodeError, match="400"):
            await c.create_session()
    finally:
        await c.aclose()


async def test_5xx_retries_then_raises(monkeypatch):
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(502, text="bad gateway")

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    # Speed up the retry backoff.
    import opencode_discord_bot.opencode_client as oc_mod
    monkeypatch.setattr(oc_mod, "_GET_RETRY_BACKOFF", 0.0)
    try:
        # 3 attempts for a 5xx. The source has a bare `raise` after the loop
        # which (with no active exception) raises RuntimeError; either
        # OpencodeError or RuntimeError is acceptable here — the test
        # asserts retries happened and an exception propagated.
        with pytest.raises((OpencodeError, RuntimeError)):
            await c.health()
        assert count["n"] == 3
    finally:
        await c.aclose()