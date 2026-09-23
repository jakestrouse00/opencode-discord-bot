"""Async httpx wrapper over the opencode **v2** server REST API (v2.0+).

opencode 2.x replaced the v1 API surface (`/session`, `/event`, ...) with a
new one under the ``/api/`` prefix with different routes, request bodies,
response envelopes, and an SSE stream whose semantic type lives in the JSON
body under ``type`` with the payload under ``data``. This module is a thin
typed surface over the v2 endpoints (route names verified against the
2.0.15 server's ``/openapi.json``).

**v1-shape projection (compatibility layer).** The bot's UI/rendering
modules (`questions.py`, `bridge_questions.py`, `monitor.py`,
`text_utils.py`, `events.py`) were written against the v1 client shapes, so
this client projects v2 responses into v1-compatible shapes:

- Response envelopes (``{"data": ...}`` / ``{"location": ..., "data": ...}``
  / ``{"data": ..., "cursor": ...}``) are unwrapped.
- ``list_messages`` returns the v1 ``{"info": ..., "parts": [...]}`` entries
  (v2's ``Session.Message.*`` shapes are translated: ``content`` parts array
  → ``parts``; user ``text`` field → a text part; tool ``name`` →
  ``part["tool"]``; the ``idle`` sentinel messages are dropped).
- ``send_prompt_async`` keeps the v1 signature (``parts``, ``agent``,
  ``model``) and maps it onto v2 primitives: per-session agent/model switch
  (``POST /api/session/{id}/agent`` + ``/model``) then
  ``POST /api/session/{id}/prompt {text}`` (v2's only prompt endpoint — it
  always schedules asynchronously).
- ``list_questions`` projects v2 ``Form.Info`` entries into the v1 question
  shape (``{id, sessionID, questions: [{question, options, multiple}]}``)
  so the button UI keeps rendering; ``reply_question`` /
  ``reject_question`` reverse-map to the v2 form reply/cancel endpoints.
- ``get_session_status`` projects the v2 ``GET /api/session/active`` map
  into the v1 ``{sessionID: {"type": "busy"}}`` shape (the v1 server removed
  idle entries from the map; so does v2's active map).

The server is spawned and torn down by `OpencodeBot` (`on_connect`/`close`)
via `opencode_discord_bot/opencode_serve.py`; this client just talks to
whatever server is at `config.opencode_server_url`.

Auth: if `opencode_server_password` is set (via `config` / `.env` / env
var), basic auth is applied to every request (username defaults to
`opencode`, overridable via `opencode_server_username`). `OpencodeBot.on_connect`
seeds that env var from `config.opencode_server_password` before starting
the server, so the client and server share the same password by default.
See https://opencode.ai/docs/server#authentication.

The SSE stream (`stream_events`) is intentionally NOT retried mid-stream by
this client — a dropped SSE connection should be re-established by the caller,
which owns the reconnect/backoff loop. The bot does NOT use the SSE stream;
it polls `get_session_status` via `opencode_discord_bot.events.poll_until_idle`.
`stream_events` speaks the v2 wire format (``{id, type, data}`` frames) and
yields the v1 consumer shape for external consumers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from opencode_discord_bot.config import config

_log = logging.getLogger("bot.opencode_client")


def _auth() -> tuple[str, str] | None:
    """Basic-auth tuple if OPENCODE_SERVER_PASSWORD is set, else None.

    Reads the password from `config.opencode_server_password` (loaded from
    `.env` / env vars by pydantic-settings). `OpencodeBot.on_connect` also
    seeds `os.environ["OPENCODE_SERVER_PASSWORD"]` so the spawned
    `opencode serve` subprocess inherits it; we fall back to that env var
    so a process that mutated the env post-startup is still honored. The
    username comes from `config.opencode_server_username` (default
    "opencode"), with the env var as a back-compat fallback.
    """
    password = config.opencode_server_password or os.environ.get(
        "OPENCODE_SERVER_PASSWORD"
    )
    if not password:
        return None
    username = (
            config.opencode_server_username
            or os.environ.get("OPENCODE_SERVER_USERNAME")
            or "opencode"
    )
    return (username, password)


# Retry config for idempotent GETs (see `OpencodeClient._request_get_with_retry`).
_GET_RETRY_ATTEMPTS = 3
_GET_RETRY_BACKOFF = 0.5  # seconds; linear backoff: 0.5s, 1.0s, ...

_STATUS_RE = re.compile(r"-> (\d{3})")


def _is_5xx_error(err: OpencodeError) -> bool:
    """True if the OpencodeError message encodes a 5xx server status.

    The error message is shaped like ``"GET /path -> 502: ..."`` (see
    `_do_request`); parse the embedded status code to decide retryability.
    """
    m = _STATUS_RE.search(str(err))
    if not m:
        return False
    try:
        return 500 <= int(m.group(1)) < 600
    except ValueError:
        return False


class OpencodeError(Exception):
    """Raised on non-2xx responses from the opencode server."""


def _unwrap_data(result: Any) -> Any:
    """Unwrap the v2 ``{"data": ...}`` response envelope."""
    if isinstance(result, dict) and set(result.keys()) == {"data"}:
        return result["data"]
    return result


def _unwrap_envelope(result: Any, key: str = "data") -> Any:
    """Unwrap the v2 list-envelope ``{"location": ..., "data": [...]}``."""
    if isinstance(result, dict) and key in result:
        return result[key]
    return result


def _model_ref_obj(model_id: str | None) -> dict | None:
    """Convert a ``"providerID/modelID"`` config string to the v2
    ``{"providerID": ..., "id": ...}`` ``Model.Ref`` object.

    Returns ``None`` for empty/None. If the string has no ``/``, logs a
    warning and returns ``None``. Splits on the FIRST ``/`` only so a model
    id containing ``/`` is preserved.
    """
    if not model_id:
        return None
    if "/" not in model_id:
        _log.warning(
            "model %r has no '/' — can't build ModelRef {providerID, "
            "id}; omitting model",
            model_id,
        )
        return None
    provider_id, _, model_id_part = model_id.partition("/")
    return {"providerID": provider_id, "id": model_id_part}


# --- v2 -> v1 message projection -------------------------------------------


def _project_tool_state_to_v1_part(part: dict) -> dict:
    """Translate one v2 assistant tool-content entry into a v1-style part.

    v2 tool content: ``{type: "tool", id, name, state: {status, input,
    content?, metadata?, error?}, time}``. v1 parts used
    ``{"type": "tool", "tool": <name>, "callID": <id>, "state": {...}}``.
    The v2 ``state`` object already carries ``status`` / ``input`` keys the
    bot's UI reads, so it's passed through; ``tool`` and ``callID`` are
    mapped from ``name`` / ``id``.
    """
    state = part.get("state") or {}
    return {
        "type": "tool",
        "tool": part.get("name"),
        "callID": part.get("id"),
        "state": state,
    }


def _project_content_to_parts(content: Any) -> list[dict]:
    """Translate the v2 assistant ``content`` array into v1-style parts."""
    parts: list[dict] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        ctype = item.get("type")
        if ctype == "text":
            parts.append({"type": "text", "text": item.get("text", "")})
        elif ctype == "reasoning":
            parts.append({"type": "reasoning", "text": item.get("text", "")})
        elif ctype == "tool":
            parts.append(_project_tool_state_to_v1_part(item))
        else:
            # Unknown content type: pass through with its v2 shape so new
            # payloads surface instead of vanishing.
            parts.append(item)
    return parts


def _project_message(entry: dict) -> dict | None:
    """Project one v2 ``Session.Message.*`` into the v1 ``{info, parts}`` shape.

    Returns None for v2-only bookkeeping entries (the ``idle`` sentinel
    emitted at turn end — v1 consumers don't know it) and agent/model/
    location "switched" markers.
    """
    mtype = entry.get("type")
    if mtype in {"idle", "agent-switched", "model-switched", "location-switched"}:
        return None
    if mtype == "assistant":
        info = {
            "id": entry.get("id"),
            "role": "assistant",
            "agent": entry.get("agent"),
            "model": entry.get("model"),
            "finish": entry.get("finish"),
            "error": entry.get("error"),
            "cost": entry.get("cost"),
            "tokens": entry.get("tokens"),
            "time": entry.get("time"),
        }
        content = entry.get("content") or []
        return {
            "info": info,
            "parts": _project_content_to_parts(
                content if isinstance(content, list) else []
            ),
        }
    if mtype == "user":
        info = {"id": entry.get("id"), "role": "user", "time": entry.get("time")}
        text = entry.get("text", "")
        return {"info": info, "parts": [{"type": "text", "text": text}]}
    # synthetic / system / skill / shell / compaction: keep the raw entry as
    # parts (best-effort text extraction still works via `_extract_text`).
    info = {
        "id": entry.get("id"),
        "role": mtype,
        "time": entry.get("time"),
    }
    parts: list[dict] = []
    text = entry.get("text")
    if isinstance(text, str):
        parts.append({"type": "text", "text": text})
    return {"info": info, "parts": parts}


def _project_form_to_v1_question(form: dict) -> dict:
    """Project one v2 ``Form.Info`` into the v1 question-request shape.

    v1 shape: ``{id, sessionID, questions: [{question, options, multiple}]}``
    (one entry per form field). The bot's button UI + the monitor's
    ``question_block`` render this shape, so the projection keeps them
    unchanged. Field options in v2 are ``{label, ...}`` objects (or bare
    strings); both are normalized to the label string.
    """
    fields = form.get("fields") or []
    questions: list[dict] = []
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict):
            continue
        raw_options = f.get("options") or []
        options = [
            o.get("label") if isinstance(o, dict) else o for o in raw_options
        ]
        questions.append(
            {
                "question": f.get("title") or f.get("key", ""),
                "options": options,
                "multiple": f.get("type") == "multiselect",
            }
        )
    return {
        "id": form.get("id"),
        "sessionID": form.get("sessionID"),
        # The form's own title, for renderers that want a headline.
        "title": form.get("title"),
        "questions": questions,
        # Keep the raw v2 shape under a namespaced key for callers that
        # need the fields (e.g. free-text forms with no options).
        "_v2_form": form,
    }


def _project_v1_question_to_v2(form: dict, answers: list[list[str]]) -> Any:
    """Build the v2 ``Form.Reply`` answer from a v1 answers array.

    The v1 ``question`` API answered ``list[list[str]]`` (one selection
    array per question); v2 forms are single-field with a scalar
    ``Form.Value``, so the first selection of the first array is sent
    (``"a"`` for ``[["a", "b"]]``). Multiselect answers are sent as the
    string array.
    """
    fields = (form.get("fields") or []) if isinstance(form, dict) else []
    if fields and isinstance(fields[0], dict) and fields[0].get("type") == "multiselect":
        first = answers[0] if answers else []
        return [str(x) for x in first]
    first = answers[0] if answers else []
    return first[0] if first else ""


class OpencodeClient:
    """Async REST + SSE client over an `opencode serve` instance.

    The base URL and basic-auth come from `config.opencode_server_url` and
    the `OPENCODE_SERVER_PASSWORD` env var respectively. One `httpx.AsyncClient`
    is lazily created per `OpencodeClient` instance and reused for the process
    lifetime — callers should construct a single client and share it.
    """

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base_url = base_url or config.opencode_server_url
        self._client: httpx.AsyncClient | None = None
        # Guards lazy `httpx.AsyncClient` construction so two concurrent
        # `_ac()` awaits don't each build a client (the loser's would leak).
        # Mirrors `opencode-rest-client`'s `client.py`.
        self._client_lock: asyncio.Lock = asyncio.Lock()

    async def _ac(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                # Double-checked locking: a second awaiter that entered the
                # lock after the first built the client must not rebuild it.
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        auth=_auth(),
                        # `read=60` bounds how long a single REST read may stall —
                        # without it a hung server would block the pollers
                        # indefinitely. The SSE stream (`stream_events`) overrides
                        # this per-request with `timeout=None` since it's a long-lived
                        # connection.
                        timeout=httpx.Timeout(
                            connect=10.0, read=60.0, write=30.0, pool=30.0
                        ),
                        headers={"Accept": "application/json"},
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- low-level helpers ---

    async def _request(
            self,
            method: str,
            path: str,
            *,
            directory: str | None = None,
            **kw: Any,
    ) -> Any:
        # Optional multi-project routing: when directory is set, inject the
        # x-opencode-directory request header so opencode's workspace-routing
        # middleware resolves the session to that project dir instead of the
        # server's process.cwd(). Mirrors opencode-rest-client's client.py.
        # No call site in the bot passes directory today (the bot pins the
        # project via OPENCODE_SERVE_CWD), so this is forward-compatible and
        # a no-op when None.
        if directory is not None:
            headers = dict(kw.get("headers") or {})
            headers["x-opencode-directory"] = directory
            kw["headers"] = headers
        client = await self._ac()
        # Idempotent GETs get a small retry on transient transport errors or
        # 5xx responses — a single blip from the opencode server shouldn't
        # kill a long-running poll loop. POSTs are never retried (not
        # idempotent; a re-send could duplicate a prompt or reply).
        if method == "GET":
            return await self._request_get_with_retry(client, method, path, **kw)
        return await self._do_request(client, method, path, **kw)

    async def _do_request(
            self, client: httpx.AsyncClient, method: str, path: str, **kw: Any
    ) -> Any:
        resp = await client.request(method, path, **kw)
        if resp.status_code >= 400:
            raise OpencodeError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def _request_get_with_retry(
            self, client: httpx.AsyncClient, method: str, path: str, **kw: Any
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(_GET_RETRY_ATTEMPTS):
            try:
                return await self._do_request(client, method, path, **kw)
            except OpencodeError as e:
                # Only retry on 5xx (server-side transient); 4xx is a real
                # client error (auth, not-found, bad-request) and should surface.
                if not _is_5xx_error(e):
                    raise
                last_exc = e
            except httpx.TransportError as e:
                # Connection reset, read timeout, etc. — transient by nature.
                last_exc = e
            if attempt < _GET_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_GET_RETRY_BACKOFF * (attempt + 1))
        # Exhausted retries; re-raise the last transient error.
        if isinstance(last_exc, OpencodeError):
            raise
        if last_exc is not None:
            raise last_exc
        raise OpencodeError(f"{method} {path} -> exhausted retries")

    # --- global ---

    async def health(self) -> dict:
        """Liveness probe (v2-shape projected to the v1 call shape).

        v2 exposes no dedicated JSON health endpoint — the OpenAPI spec
        (``GET /openapi.json``, 200 with auth) doubles as the probe.
        Returns ``{"healthy": True, "version": <spec version>}``.
        """
        spec = await self._request("GET", "/openapi.json")
        info: dict = {}
        if isinstance(spec, dict):
            info = {"version": (spec.get("info") or {}).get("version")}
        return {"healthy": True, **info}

    # --- sessions ---

    async def list_sessions(
            self, *, directory: str | None = None
    ) -> list[dict]:
        """GET /api/session — all sessions (newest-first), ``{"data": [...]}``."""
        kw: dict[str, Any] = {}
        if directory is not None:
            kw["params"] = {"directory": directory}
        result = await self._request("GET", "/api/session", **kw)
        data = _unwrap_envelope(result)
        return data if isinstance(data, list) else []

    async def create_session(
            self, title: str | None = None, *, directory: str | None = None
    ) -> dict:
        """POST /api/session — body `{ title? }`, returns the new session."""
        body: dict = {}
        if title:
            body["title"] = title
        result = await self._request(
            "POST", "/api/session", json=body, directory=directory
        )
        return _unwrap_data(result) or {}

    async def get_session(
            self, sid: str, *, directory: str | None = None
    ) -> dict:
        """GET /api/session/{id} — session details (``{"data": Session}``)."""
        result = await self._request(
            "GET", f"/api/session/{sid}", directory=directory
        )
        return _unwrap_data(result) or {}

    async def delete_session(
            self, sid: str, *, directory: str | None = None
    ) -> bool:
        """DELETE /api/session/{id} — 204/200 on success."""
        await self._request(
            "DELETE", f"/api/session/{sid}", directory=directory
        )
        return True

    async def abort_session(
            self, sid: str, *, directory: str | None = None
    ) -> bool:
        """POST /api/session/{id}/interrupt — abort a running session.

        Returns ``interrupted`` from the response (True when an active
        execution was interrupted, False for the idle no-op).
        """
        result = await self._request(
            "POST", f"/api/session/{sid}/interrupt", directory=directory
        )
        if isinstance(result, dict):
            return bool(result.get("interrupted", True))
        # 204 (no body) also means the interrupt was processed.
        return True

    async def revert_session(
            self, sid: str, message_id: str, *, directory: str | None = None
    ) -> dict:
        """Stage + commit a revert to a user message (v2 two-step).

        v2 splits the v1 ``revert`` into stage (``POST .../revert/stage``
        with ``{"messageID"}``) + commit (``POST .../revert``). Issuing both
        back-to-back preserves the v1 one-shot semantics. Requires the
        session to NOT be busy (v2 409s on a busy session), so callers
        should abort + wait first.
        """
        await self._request(
            "POST",
            f"/api/session/{sid}/revert/stage",
            json={"messageID": message_id},
            directory=directory,
        )
        result = await self._request(
            "POST", f"/api/session/{sid}/revert", directory=directory
        )
        return _unwrap_data(result) or {}

    async def get_session_status(
            self, *, directory: str | None = None
    ) -> dict[str, dict]:
        """GET /api/session/active — running-session map (v1 shape projected).

        v2 has no ``/session/status``; ``/api/session/active`` returns
        ``{"data": {sessionID: {"type": "running"}}}`` for sessions with an
        active agent loop — sessions absent from the map are idle. The v1
        shape (``{sessionID: {"type": "busy"|"idle"|"retry", ...}}``) is
        projected: present → ``{"type": "busy"}``; absent ids are idle (the
        v1 server also removed idle entries from the map, so the callers'
        missing-entry-is-idle logic is unchanged).

        ``directory`` routes the request to that project instance.
        """
        result = await self._request(
            "GET", "/api/session/active", directory=directory
        )
        data = _unwrap_data(result)
        if not isinstance(data, dict):
            return {}
        # Project v2 {"type": "running"} → the v1 {"type": "busy"} shape
        # (poll_until_idle's saw_busy check keys on "busy").
        return {sid: {"type": "busy"} for sid in data}

    async def list_projects(self) -> list[dict]:
        """GET /api/project — the server's known project instances.

        The session monitor uses the worktree/directory values as the
        directory fan-out for its per-instance polls.
        """
        result = await self._request("GET", "/api/project")
        data = _unwrap_data(result)
        return data if isinstance(data, list) else []

    # --- messages ---

    async def list_messages(
            self, sid: str, limit: int | None = None, *, directory: str | None = None
    ) -> list[dict]:
        """GET /api/session/{id}/message — projected `{ info, parts }[]`.

        v2 returns a paginated ``{"data": [...], "cursor": {...}}`` envelope
        of ``Session.Message.*`` entries; this method unwraps the envelope,
        projects each entry to the v1 ``{info, parts}`` shape, and drops the
        v2-only ``idle`` sentinel entries.
        """
        params: dict = {}
        if limit is not None:
            params["limit"] = limit
        result = await self._request(
            "GET", f"/api/session/{sid}/message", params=params, directory=directory
        )
        data = _unwrap_envelope(result)
        out: list[dict] = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            projected = _project_message(entry)
            if projected is not None:
                out.append(projected)
        return out

    def _resolve_model(self, agent: str | None) -> str | None:
        """Pick the model to send for a prompt, based on the agent + config.

        Returns the configured override model id (non-empty) for the agent as
        a ``"providerID/modelID"`` string, or None to let the opencode server
        fall back to the agent's frontmatter ``model:`` field (the historical
        behavior when both overrides are empty). Two separate config fields
        cover the two agent surfaces the bot uses: ``opencode_default_model``
        for ``agent=None`` (``/oc`` + plain-text follow-ups) and
        ``opencode_assistant_model`` for ``agent="oc-assistant"``
        (``/oc_plan`` / ``/oc_voice`` / ``/oc_talk`` / voice-message trigger /
        Comulytic bridge). Other agent names (custom agents, if any) fall
        under the default field too — there's no per-agent override beyond
        these two.

        The returned string is NOT sent on the wire as-is: ``send_message``
        and ``send_prompt_async`` pass it through ``_model_ref`` to build the
        ``{"providerID", "id"}`` object the v2 ``Model.Ref`` requires (a
        string is rejected with HTTP 400). The config format stays as the
        convenient ``"providerID/modelID"`` string; the split is an internal
        detail of this client.
        """
        if agent == "oc-assistant":
            return config.opencode_assistant_model or None
        return config.opencode_default_model or None

    @staticmethod
    def _model_ref(model_id: str | None) -> dict | None:
        """Convert a ``"providerID/modelID"`` config string to the v2
        ``{"providerID": ..., "id": ...}`` ``Model.Ref`` object.

        Returns ``None`` for empty/None so the caller omits the key entirely
        and the session's default model wins. If the string has no ``/``,
        logs a warning and returns ``None`` (a valid ``Model.Ref`` needs both
        parts; falling back to the session default is safer than sending a
        malformed object that 400s). Splits on the FIRST ``/`` only so a
        model id containing ``/`` is preserved.
        """
        return _model_ref_obj(model_id)

    async def send_message(
            self,
            sid: str,
            parts: list[dict],
            agent: str | None = None,
            model: str | None = None,
            *,
            directory: str | None = None,
    ) -> dict:
        """Synchronous prompt on v2: dispatch → wait → newest assistant turn.

        The bot itself uses ``send_prompt_async`` (fire-and-forget) + status
        polling everywhere; this synchronous variant is part of the typed
        REST surface for external consumers who want blocking semantics.

        v2's ``POST /api/session/{id}/prompt`` only admits the input and
        schedules the agent loop (it returns the admitted user inbox entry,
        NOT the assistant reply), and its body takes ``{text, files, ...}``
        — no ``agent``/``model``/``parts``. This method preserves the v1
        sync signature by: switching the session agent/model when overrides
        are given → sending the text of the first text part as the prompt →
        blocking on ``/api/experimental/session/{id}/wait`` (204 when idle)
        → returning the newest assistant message in the v1 ``{info, parts}``
        shape. Only the first ``{"type": "text"}`` part's text is sent.
        """
        text = next(
            (p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"),
            "",
        )
        if not text:
            raise OpencodeError(
                "send_message requires at least one {'type': 'text'} part "
                "with non-empty text"
            )
        # Apply per-session agent/model overrides when configured.
        if agent:
            await self._request(
                "POST", f"/api/session/{sid}/agent",
                json={"agent": agent}, directory=directory,
            )
        resolved_model = model if model is not None else self._resolve_model(agent)
        model_ref = self._model_ref(resolved_model)
        if model_ref is not None:
            await self._request(
                "POST", f"/api/session/{sid}/model",
                json={"model": model_ref}, directory=directory,
            )
        await self.send_prompt_async(
            sid, parts, directory=directory, _override_prompt_text=text
        )
        await self.wait_session_idle(sid)
        # The newest assistant message is the turn's reply.
        messages = await self.list_messages(sid, directory=directory)
        for entry in reversed(messages):
            if isinstance(entry.get("info"), dict) and entry["info"].get("role") == "assistant":
                return entry
        return {}

    async def send_prompt_async(
            self,
            sid: str,
            parts: list[dict],
            agent: str | None = None,
            model: str | None = None,
            *,
            directory: str | None = None,
            _override_prompt_text: str | None = None,
    ) -> None:
        """POST /api/session/{id}/prompt — admit the input, schedule the loop.

        v2 has no separate ``prompt_async``; the single ``prompt`` endpoint
        always schedules asynchronously and returns immediately with the
        admitted user inbox entry. Pair with `get_session_status` polling
        (see `opencode_discord_bot.events.poll_until_idle`) to observe
        progress and the final result.

        `agent` / `model` override the session's agent/model for subsequent
        turns (v2 semantics: per-session via ``/api/session/{id}/agent`` +
        ``/model``). When ``model is None`` (the common case), it's resolved
        from config via ``_resolve_model`` so the bot's
        ``OPENCODE_DEFAULT_MODEL`` / ``OPENCODE_ASSISTANT_MODEL`` env vars
        flow through transparently; no request is made when both the
        explicit and resolved values are empty (the session's own default
        wins).
        """
        # Per-session agent/model switch (only when an override applies —
        # otherwise the session's own default wins, matching v1 behavior).
        if agent:
            await self._request(
                "POST", f"/api/session/{sid}/agent",
                json={"agent": agent}, directory=directory,
            )
        resolved_model = model if model is not None else self._resolve_model(agent)
        model_ref = self._model_ref(resolved_model)
        if model_ref is not None:
            await self._request(
                "POST", f"/api/session/{sid}/model",
                json={"model": model_ref}, directory=directory,
            )
        text = _override_prompt_text or next(
            (
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ),
            "",
        )
        if not text:
            raise OpencodeError(
                "send_prompt_async requires at least one {'type': 'text'} "
                "part with non-empty text"
            )
        await self._request(
            "POST", f"/api/session/{sid}/prompt",
            json={"text": text}, directory=directory,
        )

    async def wait_session_idle(
            self, sid: str, *, timeout: float = 1800.0
    ) -> None:
        """POST /api/experimental/session/{id}/wait — block until idle (204).

        Server-side blocking wait for the session's agent loop to go idle
        (the v2 replacement for client-side ``poll_until_idle`` loops).
        httpx's read timeout (60s) applies; the bot's polling loop
        (`poll_until_idle`) remains the primary idle signal — this is for
        external consumers who want blocking semantics.
        """
        await self._request(
            "POST", f"/api/experimental/session/{sid}/wait", timeout=timeout
        )

    # --- questions (v2 forms, projected to the v1 question shape) ---

    async def list_questions(
            self, *, directory: str | None = None
    ) -> list[dict]:
        """GET /api/form — pending forms, projected to the v1 question shape.

        v2 replaced the v1 ``question`` API with **forms**; each
        ``Form.Info`` (``{id, sessionID, title, fields}``) is projected into
        the v1 request shape (``{id, sessionID, questions: [{question,
        options, multiple}]}``, one entry per form field) so the bot's
        button UI + the monitor's ``question_block`` render unchanged. The
        raw v2 ``Form.Info`` is preserved under ``_v2_form`` for callers
        that need the fields directly. The bot filters by ``sessionID`` to
        surface only those for the session it's driving.

        ``directory`` routes the request to that project instance.
        """
        kw: dict[str, Any] = {}
        if directory is not None:
            kw["params"] = {"location[directory]": directory}
        result = await self._request("GET", "/api/form", **kw)
        data = _unwrap_envelope(result)
        out: list[dict] = []
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict):
                out.append(_project_form_to_v1_question(entry))
        return out

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        """POST /api/session/{id}/form/{formID}/reply — answer a pending form.

        Resolves the form from the pending list by id, then submits the
        answer (the v1 ``answers`` list[list[str]] is reverse-mapped to the
        v2 ``Form.Value`` — the first selection of the first array, or the
        full array for multiselect fields). Resolves the deferred the
        agent's form is awaiting, so the agent turn resumes. Returns
        ``True`` on success (204).
        """
        for req in await self.list_questions():
            if req.get("id") == request_id:
                v2_form = req.get("_v2_form") or req
                sid = v2_form.get("sessionID") or req.get("sessionID")
                fid = v2_form.get("id") or request_id
                answer = _project_v1_question_to_v2(v2_form, answers)
                await self._request(
                    "POST",
                    f"/api/session/{sid}/form/{fid}/reply",
                    json={"answer": answer},
                )
                return True
        _log.warning("form %s not in pending list; cannot reply", request_id)
        return False

    async def reject_question(self, request_id: str) -> bool:
        """DELETE /api/session/{id}/form/{formID} — cancel a pending form.

        v2's replacement for the v1 question reject: the form's deferred
        resolves "cancelled" instead of parking until server restart. Used
        on session timeout/abort for surfaced-but-unanswered requests so the
        agent gets a clean dismissal.
        """
        for req in await self.list_questions():
            if req.get("id") == request_id:
                v2_form = req.get("_v2_form") or req
                sid = v2_form.get("sessionID") or req.get("sessionID")
                fid = v2_form.get("id") or request_id
                if not sid or not fid:
                    return False
                await self._request("DELETE", f"/api/session/{sid}/form/{fid}")
                return True
        _log.warning("form %s not in pending list; cannot cancel", request_id)
        return False

    # --- permissions ---

    async def list_permissions(
            self, *, directory: str | None = None
    ) -> list[dict]:
        """GET /api/permission/request — pending permission requests.

        Each entry is a v2 ``Permission.Request``
        (``{id, sessionID, action, resources, ...}``). The v1 shape also had
        a human ``permission`` name; v2's ``action`` is projected onto it so
        the button UI / monitor render unchanged.

        ``directory`` routes the request to that project instance.
        """
        kw: dict[str, Any] = {}
        if directory is not None:
            kw["params"] = {"location[directory]": directory}
        result = await self._request("GET", "/api/permission/request", **kw)
        data = _unwrap_envelope(result)
        out: list[dict] = []
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict):
                projected = dict(entry)
                # v1 renderers read ``permission``; v2 calls it ``action``.
                projected.setdefault("permission", entry.get("action"))
                projected.setdefault("patterns", entry.get("resources") or [])
                out.append(projected)
        return out

    async def reply_permission(
            self, request_id: str, reply: str, message: str | None = None
    ) -> bool:
        """POST /api/session/{id}/permission/{requestID}/reply — approve/deny.

        ``reply`` is one of ``"once"`` | ``"always"`` | ``"reject"`` (the
        same values as v1 — v2 renamed the field to ``decision``).
        ``"always"`` persists an allow-rule on the opencode server (survives
        restarts); ``"reject"`` fails the tool. The owning session is
        resolved from the pending list. Returns ``True`` on success (204).
        """
        for req in await self.list_permissions():
            if req.get("id") == request_id:
                sid = req.get("sessionID")
                if not sid:
                    return False
                body: dict = {"decision": reply}
                if message is not None:
                    body["message"] = message
                await self._request(
                    "POST",
                    f"/api/session/{sid}/permission/{request_id}/reply",
                    json=body,
                )
                return True
        _log.warning(
            "permission request %s not in pending list; cannot reply", request_id
        )
        return False

    # --- agents ---
    # Not used by the bot itself (the bot never enumerates opencode agents);
    # part of the typed REST surface for external consumers.

    async def list_agents(
            self, *, directory: str | None = None
    ) -> list[dict]:
        """GET /api/agent — all available agents (``{"location", "data"}``)."""
        kw: dict[str, Any] = {}
        if directory is not None:
            kw["params"] = {"location[directory]": directory}
        result = await self._request("GET", "/api/agent", **kw)
        data = _unwrap_envelope(result)
        return data if isinstance(data, list) else []

    # --- events (SSE) ---

    async def stream_events(self) -> AsyncGenerator[dict, None]:
        """GET /api/event — Server-Sent Events stream (global, all sessions).

        Yields parsed event dicts in the v1 consumer shape
        ``{"type": ..., "properties": {...}}``: the v2 SSE ``data:`` frame is
        JSON ``{"id", "type", "data"}`` where ``type`` is the semantic type
        (``message.part.delta``, ``session.status``, ``permission.asked``,
        ``form.created``, ...) and ``data`` is the properties payload;
        heartbeat comment frames produce no yield.

        This is a streaming generator over a single long-lived HTTP connection
        — it is NOT retried. The caller owns the reconnect/backoff loop.
        Raises `OpencodeError` if the connection fails to establish;
        `httpx.RemoteProtocolError` / `httpx.ReadError` surface to the caller
        if the stream drops mid-iteration.

        The Discord bot does NOT use this stream — it polls
        ``get_session_status`` via ``events.poll_until_idle`` as the
        authoritative idle signal (SSE is opportunistically parsed here for
        external consumers only).
        """
        client = await self._ac()
        async with client.stream(
                "GET",
                "/api/event",
                headers={"Accept": "text/event-stream"},
                # The client's default read timeout (60s) would kill a long-lived
                # SSE stream mid-iteration. Override per-request so the stream
                # stays open until the server closes it or the caller cancels.
                timeout=None,
        ) as r:
            if r.status_code >= 400:
                raise OpencodeError(
                    f"GET /api/event -> {r.status_code}: {r.text[:500]}"
                )
            data_lines: list[str] = []
            async for line in r.aiter_lines():
                if line == "":
                    # blank line = end of event
                    if data_lines:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            payload = {"raw": "\n".join(data_lines)}
                        # v2 frames: {id, type, data}; project to the v1
                        # consumer shape {type, properties}.
                        yield {
                            "type": payload.get("type", "unknown")
                            if isinstance(payload, dict)
                            else "unknown",
                            "properties": payload.get("data", payload)
                            if isinstance(payload, dict)
                            else payload,
                        }
                    data_lines = []
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
                # v2 sets no `event:` line (the semantic type is inside the
                # JSON body); comment lines (`: heartbeat`) and unknown
                # fields are ignored.
            # flush a trailing event if the stream ended without a blank line
            if data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = {"raw": "\n".join(data_lines)}
                yield {
                    "type": payload.get("type", "unknown")
                    if isinstance(payload, dict)
                    else "unknown",
                    "properties": payload.get("data", payload)
                    if isinstance(payload, dict)
                    else payload,
                }
