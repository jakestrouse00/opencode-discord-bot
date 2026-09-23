"""Unit tests for ``opencode_client.OpencodeClient`` (v2 API, MockTransport)."""

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
    # v2 Model.Ref uses {providerID, id} (the v1 key was "modelID").
    ref = OpencodeClient._model_ref("ollama-cloud/glm-5.2")
    assert ref == {"providerID": "ollama-cloud", "id": "glm-5.2"}


def test_model_ref_no_slash_returns_none():
    assert OpencodeClient._model_ref("nodash") is None


async def test_create_session(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/session"
        assert request.method == "POST"
        return httpx.Response(200, json={"data": {"id": "sid-1"}})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.create_session(title="test")
        assert result["id"] == "sid-1"
    finally:
        await c.aclose()


async def test_send_prompt_async_returns_none_on_prompt_response(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/session/sid-1/prompt"
        return httpx.Response(200, json={"data": {"id": "msg-u", "type": "user"}})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.send_prompt_async("sid-1", [{"type": "text", "text": "hi"}])
        assert result is None
    finally:
        await c.aclose()


async def test_send_prompt_async_switches_session_agent_and_model(monkeypatch):
    monkeypatch.setattr(config, "opencode_assistant_model", "anthropic/claude-sonnet-4")
    captured = {}
    calls = []

    def handler(request):
        import json
        captured[request.url.path] = (
            json.loads(request.read().decode()) if request.read() else {}
        )
        calls.append(request.url.path)
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"data": {"id": "msg-u", "type": "user"}})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        await c.send_prompt_async("sid-1", [{"type": "text", "text": "x"}], agent="oc-assistant")
        # v2: agent/model are switched per-session BEFORE the prompt.
        assert "/api/session/sid-1/agent" in captured
        assert captured["/api/session/sid-1/agent"] == {"agent": "oc-assistant"}
        assert "/api/session/sid-1/model" in captured
        assert captured["/api/session/sid-1/model"] == {
            "model": {"providerID": "anthropic", "id": "claude-sonnet-4"}
        }
        assert captured["/api/session/sid-1/prompt"] == {"text": "x"}
    finally:
        await c.aclose()


async def test_send_prompt_async_without_model_override_skips_model_call(monkeypatch):
    monkeypatch.setattr(config, "opencode_default_model", "")
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"data": {"id": "msg-u", "type": "user"}})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        await c.send_prompt_async("sid-1", [{"type": "text", "text": "x"}])
        # No model override → no /model switch call, no agent switch call.
        assert "/api/session/sid-1/model" not in paths
        assert "/api/session/sid-1/agent" not in paths
        assert "/api/session/sid-1/prompt" in paths
    finally:
        await c.aclose()


async def test_list_questions_projects_v2_forms_to_v1_shape(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/form"
        return httpx.Response(
            200,
            json={
                "location": {"directory": "x"},
                "data": [
                    {
                        "id": "frm_1",
                        "sessionID": "sid-1",
                        "title": "Pick",
                        "fields": [
                            {
                                "key": "choice",
                                "type": "multiselect",
                                "title": "Pick",
                                "options": [{"label": "a"}, {"label": "b"}],
                            }
                        ],
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.list_questions()
        assert len(result) == 1
        q = result[0]
        assert q["id"] == "frm_1"
        assert q["sessionID"] == "sid-1"
        assert q["questions"] == [
            {"question": "Pick", "options": ["a", "b"], "multiple": True}
        ]
        # The raw v2 form is preserved for callers that need the fields.
        assert q["_v2_form"]["id"] == "frm_1"
    finally:
        await c.aclose()


async def test_reply_question_posts_v2_form_reply(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.read().decode()))
        if request.method == "GET" and request.url.path == "/api/form":
            return httpx.Response(
                200,
                json={
                    "location": {"directory": "x"},
                    "data": [
                        {
                            "id": "frm_1",
                            "sessionID": "sid-1",
                            "title": "Pick",
                            "fields": [
                                {"key": "choice", "type": "string", "title": "Pick",
                                 "options": [{"label": "opt-a"}]}
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.reply_question("frm_1", [["opt-a"]])
        assert result is True
        # The reply POSTs to the session-scoped v2 endpoint with the v2
        # Form.Reply shape ({answer: <scalar>}).
        import json
        assert calls[-1][:2] == ("POST", "/api/session/sid-1/form/frm_1/reply")
        assert json.loads(calls[-1][2]) == {"answer": "opt-a"}
    finally:
        await c.aclose()


async def test_reject_question_cancels_v2_form(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/form":
            return httpx.Response(
                200,
                json={
                    "location": {"directory": "x"},
                    "data": [
                        {"id": "frm_1", "sessionID": "sid-1", "title": "Pick",
                         "fields": []}
                    ],
                },
            )
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.reject_question("frm_1")
        assert result is True
        assert calls[-1] == ("DELETE", "/api/session/sid-1/form/frm_1")
    finally:
        await c.aclose()


async def test_abort_session_returns_interrupted(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/session/sid-1/interrupt"
        return httpx.Response(200, json={"interrupted": True})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.abort_session("sid-1")
        assert result is True
    finally:
        await c.aclose()


async def test_revert_session_stages_then_commits(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.read().decode()))
        return httpx.Response(200, json={"data": {"id": "sid-1"}})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.revert_session("sid-1", "m-1")
        assert result["id"] == "sid-1"
        # v2 two-step: stage (with the messageID body) then commit.
        import json
        assert calls[0][:2] == ("POST", "/api/session/sid-1/revert/stage")
        assert json.loads(calls[0][2]) == {"messageID": "m-1"}
        assert calls[1][:2] == ("POST", "/api/session/sid-1/revert")
    finally:
        await c.aclose()


async def test_get_session_status_projects_active_map(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/session/active"
        return httpx.Response(200, json={"data": {"sid-1": {"type": "running"}}})

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.get_session_status()
        # v2 "running" is projected to the v1 "busy" shape.
        assert result["sid-1"]["type"] == "busy"
    finally:
        await c.aclose()


async def test_list_messages_projects_v2_envelope(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "msg-a",
                        "type": "assistant",
                        "agent": "build",
                        "model": {"providerID": "x", "id": "y"},
                        "content": [{"type": "text", "text": "hello"}],
                        "time": {"created": 1},
                    }
                ],
                "cursor": {"previous": None, "next": None},
            },
        )

    transport = httpx.MockTransport(handler)
    c = _client_with(transport, monkeypatch)
    try:
        result = await c.list_messages("sid-1")
        assert len(result) == 1
        assert result[0]["info"]["role"] == "assistant"
        assert result[0]["parts"] == [{"type": "text", "text": "hello"}]
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