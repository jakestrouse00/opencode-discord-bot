"""Async httpx wrapper over the opencode server REST API.

There is no official Python SDK for the opencode server (the SDK is JS/TS-only
per https://opencode.ai/docs/sdk), so this module is a thin typed surface over
the REST endpoints documented at https://opencode.ai/docs/server. All methods
are async and operate on a single `httpx.AsyncClient` owned for the process
lifetime; the client is created lazily so importing this module does not open
any connection.

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
which owns the reconnect/backoff loop. Note: the Discord bot no longer uses
the SSE stream; it polls `GET /session/status` via `get_session_status` (see
`opencode_discord_bot.events.poll_until_idle`) because the v2 SSE wire format
proved fragile to parse. `stream_events` is retained for other potential
consumers.
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

    async def _ac(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=_auth(),
                # `read=60` bounds how long a single REST read may stall —
                # without it a hung server would block the pollers
                # indefinitely. The SSE stream (`stream_events`) overrides
                # this per-request with `timeout=None` since it's a long-lived
                # connection.
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=30.0),
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- low-level helpers ---

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
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
        """GET /global/health — `{ healthy, version }`."""
        return await self._request("GET", "/global/health")

    # --- sessions ---

    async def list_sessions(self) -> list[dict]:
        """GET /session — all sessions."""
        return await self._request("GET", "/session")

    async def create_session(self, title: str | None = None) -> dict:
        """POST /session — body `{ title? }`, returns the new session."""
        body: dict = {}
        if title:
            body["title"] = title
        return await self._request("POST", "/session", json=body)

    async def get_session(self, sid: str) -> dict:
        """GET /session/{id} — session details."""
        return await self._request("GET", f"/session/{sid}")

    async def delete_session(self, sid: str) -> bool:
        """DELETE /session/{id} — returns bool."""
        return bool(await self._request("DELETE", f"/session/{sid}"))

    async def abort_session(self, sid: str) -> bool:
        """POST /session/{id}/abort — abort a running session, returns bool."""
        return bool(await self._request("POST", f"/session/{sid}/abort"))

    async def revert_session(self, sid: str, message_id: str) -> dict:
        """POST /session/{id}/revert — revert to a user message, returns session.

        Body ``{"messageID": message_id}`` reverts the session to before the
        given user message, undoing its file changes and removing all
        subsequent messages (server-side, via ``SessionRevert.revert``). The
        server's next ``prompt`` handler auto-cleans the reverted state
        (``packages/opencode/src/session/prompt.ts`` ``revert.cleanup``), so no
        separate "commit" step is needed — just revert then send the new
        prompt. Requires the session to NOT be busy (``assertNotBusy`` in
        ``revert.ts``); a busy session raises ``OpencodeError`` (HTTP 400
        ``SessionBusyError``), so callers should abort + wait first.
        """
        return await self._request(
            "POST",
            f"/session/{sid}/revert",
            json={"messageID": message_id},
        )

    async def get_session_status(self) -> dict[str, dict]:
        """GET /session/status — ``{ sessionID: SessionStatus }``.

        Each value is a ``SessionStatus`` object whose ``type`` is one of
        ``"idle"`` | ``"busy"`` | ``"retry"``. A missing entry for a session id
        means that session is idle (the server removes idle entries from the
        status map). See ``packages/schema/src/session-status-event.ts`` and
        ``packages/opencode/src/server/routes/instance/httpapi/groups/session.ts``.
        """
        result = await self._request("GET", "/session/status")
        return result if isinstance(result, dict) else {}

    # --- messages ---

    async def list_messages(self, sid: str, limit: int | None = None) -> list[dict]:
        """GET /session/{id}/message — `{ info, parts }[]`."""
        params: dict = {}
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", f"/session/{sid}/message", params=params)

    def _resolve_model(self, agent: str | None) -> str | None:
        """Pick the model to send for a prompt, based on the agent + config.

        Returns the configured override model id (non-empty) for the agent as
        a ``"providerID/modelID"`` string, or None to let the opencode server
        fall back to the agent's frontmatter ``model:`` field (the historical
        behavior when both overrides are empty). Two separate config fields
        cover the two agent surfaces the bot uses: ``opencode_default_model``
        for ``agent=None`` (``/oc`` + plain-text follow-ups) and
        ``opencode_plan_author_model`` for ``agent="plan-author"``
        (``/oc_plan`` / ``/oc_voice`` / ``/oc_talk`` / voice-message trigger /
        Comulytic bridge). Other agent names (custom agents, if any) fall
        under the default field too — there's no per-agent override beyond
        these two.

        The returned string is NOT sent on the wire as-is: ``send_message``
        and ``send_prompt_async`` pass it through ``_model_ref`` to build the
        ``{"providerID", "modelID"}`` object the opencode server's
        ``PromptInput.model`` field requires (a string is rejected with HTTP
        400 ``Expected object | null, got "..." at ["model"]``). The config
        format stays as the convenient ``"providerID/modelID"`` string; the
        split is an internal detail of this client.
        """
        if agent == "plan-author":
            return config.opencode_plan_author_model or None
        return config.opencode_default_model or None

    @staticmethod
    def _model_ref(model_id: str | None) -> dict | None:
        """Convert a ``"providerID/modelID"`` config string to the
        ``{"providerID": ..., "modelID": ...}`` object the opencode server's
        ``prompt_async`` / ``message`` endpoints require (the server's
        ``PromptInput.model`` field is ``optional(ModelRef)`` — an object or
        absent — NOT a string).

        Returns ``None`` for empty/None so the caller omits the key entirely
        and the server falls back to the agent's frontmatter ``model:`` field.
        If the string has no ``/``, logs a warning and returns ``None`` (a
        valid ``ModelRef`` needs both parts; falling back to the agent's
        frontmatter model is safer than sending a malformed object that 400s).
        Splits on the FIRST ``/`` only so a model id containing ``/`` is
        preserved.
        """
        if not model_id:
            return None
        if "/" not in model_id:
            _log.warning(
                "model %r has no '/' — can't build ModelRef {providerID, "
                "modelID}; omitting model so the agent frontmatter model wins",
                model_id,
            )
            return None
        provider_id, _, model_id_part = model_id.partition("/")
        return {"providerID": provider_id, "modelID": model_id_part}

    async def send_message(
        self,
        sid: str,
        parts: list[dict],
        agent: str | None = None,
        model: str | None = None,
    ) -> dict:
        """POST /session/{id}/message — synchronous wait for full response.

        The bot itself uses ``send_prompt_async`` (fire-and-forget) + status
        polling everywhere; this synchronous variant is part of the typed
        REST surface for external consumers who want blocking semantics.

        `parts` is the opencode message-parts array, e.g.
        ``[{"type": "text", "text": "..."}]``. `agent` selects an opencode agent
        (e.g. ``"plan"`` for plan mode). Returns ``{ info, parts }``.

        `model` overrides the agent's frontmatter model for this one call; when
        ``model is None`` (the common case — callers don't pass it), it's
        resolved from config via ``_resolve_model`` so the bot's
        ``OPENCODE_DEFAULT_MODEL`` / ``OPENCODE_PLAN_AUTHOR_MODEL`` env vars
        flow through transparently. The resolved ``"providerID/modelID"``
        string is converted to the ``{"providerID", "modelID"}`` object the
        opencode server expects (see ``_model_ref``); the ``model`` key is
        omitted when no override is configured.
        """
        body: dict = {"parts": parts}
        if agent is not None:
            body["agent"] = agent
        resolved_model = model if model is not None else self._resolve_model(agent)
        model_ref = self._model_ref(resolved_model)
        if model_ref is not None:
            body["model"] = model_ref
        return await self._request("POST", f"/session/{sid}/message", json=body)

    async def send_prompt_async(
        self,
        sid: str,
        parts: list[dict],
        agent: str | None = None,
        model: str | None = None,
    ) -> None:
        """POST /session/{id}/prompt_async — fire-and-forget (204 No Content).

        Pair with `stream_events` to observe progress and the final result
        (the Discord bot uses `get_session_status` polling instead — see
        `opencode_discord_bot.events.poll_until_idle`).

        `model` overrides the agent's frontmatter model for this one call; when
        ``model is None`` (the common case — callers don't pass it), it's
        resolved from config via ``_resolve_model`` so the bot's
        ``OPENCODE_DEFAULT_MODEL`` / ``OPENCODE_PLAN_AUTHOR_MODEL`` env vars
        flow through transparently. The resolved ``"providerID/modelID"``
        string is converted to the ``{"providerID", "modelID"}`` object the
        opencode server expects (see ``_model_ref``); the ``model`` key is
        omitted when no override is configured.
        """
        body: dict = {"parts": parts}
        if agent is not None:
            body["agent"] = agent
        resolved_model = model if model is not None else self._resolve_model(agent)
        model_ref = self._model_ref(resolved_model)
        if model_ref is not None:
            body["model"] = model_ref
        await self._request("POST", f"/session/{sid}/prompt_async", json=body)

    # --- questions ---

    async def list_questions(self) -> list[dict]:
        """GET /question — all pending question requests across sessions.

        Each entry is a ``Request`` (``{ id, sessionID, questions: Info[], tool? }``)
        per ``packages/schema/src/v1/question.ts``. The bot filters by
        ``sessionID`` to surface only those for the session it's driving.
        """
        result = await self._request("GET", "/question")
        return result if isinstance(result, list) else []

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        """POST /question/:id/reply — answers in question order.

        ``answers`` is one array of selected labels per question, in order.
        Resolves the deferred the ``question`` tool is awaiting, so the agent
        turn resumes. Returns ``True`` on success.
        """
        return bool(
            await self._request(
                "POST", f"/question/{request_id}/reply", json={"answers": answers}
            )
        )

    async def reject_question(self, request_id: str) -> bool:
        """POST /question/:id/reject — fails the deferred with RejectedError.

        The ``question`` tool returns "user dismissed" to the agent. Used on
        session timeout/abort for surfaced-but-unanswered requests so the agent
        gets a clean dismissal instead of parking until server restart.
        """
        return bool(await self._request("POST", f"/question/{request_id}/reject"))

    # --- permissions ---

    async def list_permissions(self) -> list[dict]:
        """GET /permission — all pending permission requests across sessions.

        Each entry is a ``Request`` (``{ id, sessionID, permission, patterns,
        metadata, always, tool? }``) per ``packages/schema/src/v1/permission.ts``.
        """
        result = await self._request("GET", "/permission")
        return result if isinstance(result, list) else []

    async def reply_permission(
        self, request_id: str, reply: str, message: str | None = None
    ) -> bool:
        """POST /permission/:id/reply — approve/deny a permission request.

        ``reply`` is one of ``"once"`` | ``"always"`` | ``"reject"``. ``"always"``
        persists an allow-rule on the opencode server (survives restarts);
        ``"reject"`` fails the tool. Returns ``True`` on success.
        """
        body: dict = {"reply": reply}
        if message is not None:
            body["message"] = message
        return bool(
            await self._request("POST", f"/permission/{request_id}/reply", json=body)
        )

    # --- agents ---
    # Not used by the bot itself (the bot never enumerates opencode agents);
    # part of the typed REST surface for external consumers.

    async def list_agents(self) -> list[dict]:
        """GET /agent — all available agents (default + plan + custom)."""
        return await self._request("GET", "/agent")

    # --- events (SSE) ---

    async def stream_events(self) -> AsyncGenerator[dict, None]:
        """GET /event — Server-Sent Events stream (global, all sessions).

        Yields parsed event dicts ``{"type": ..., "properties": {...}}``. The
        first event is ``server.connected``, then bus events such as
        ``session.updated`` and ``message.updated``.

        This is a streaming generator over a single long-lived HTTP connection
        — it is NOT retried. The caller owns the reconnect/backoff loop. Raises `OpencodeError` if the connection fails to
        establish; `httpx.RemoteProtocolError` / `httpx.ReadError` surface to
        the caller if the stream drops mid-iteration.
        """
        client = await self._ac()
        async with client.stream(
            "GET",
            "/event",
            headers={"Accept": "text/event-stream"},
            # The client's default read timeout (60s) would kill a long-lived
            # SSE stream mid-iteration. Override per-request so the stream
            # stays open until the server closes it or the caller cancels.
            timeout=None,
        ) as r:
            if r.status_code >= 400:
                raise OpencodeError(f"GET /event -> {r.status_code}: {r.text[:500]}")
            event_type: str | None = None
            data_lines: list[str] = []
            async for line in r.aiter_lines():
                if line == "":
                    # blank line = end of event
                    if event_type is not None and data_lines:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            payload = {"raw": "\n".join(data_lines)}
                        yield {"type": event_type, "properties": payload}
                    event_type = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip())
                # ignore comment lines (:) and unknown fields
            # flush a trailing event if the stream ended without a blank line
            if event_type is not None and data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = {"raw": "\n".join(data_lines)}
                yield {"type": event_type, "properties": payload}
