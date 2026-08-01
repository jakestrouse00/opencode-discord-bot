"""Async httpx wrapper over the opencode server REST API.

There is no official Python SDK for the opencode server (the SDK is JS/TS-only
per https://opencode.ai/docs/sdk), so this module is a thin typed surface over
the REST endpoints documented at https://opencode.ai/docs/server. All methods
are async and operate on a single `httpx.AsyncClient` owned for the process
lifetime; the client is created lazily so importing this module does not open
any connection.

The server is spawned and torn down by `OpencodeBot` (setup_hook/close) via
`bot/opencode_serve.py`; this client just talks to whatever server is at
`config.opencode_server_url`.

Auth: if `OPENCODE_SERVER_PASSWORD` is set in the environment, basic auth is
applied to every request (username defaults to `opencode`, overridable via
`OPENCODE_SERVER_USERNAME`). `OpencodeBot.setup_hook` seeds that env var from
`config.opencode_server_password` before starting the server, so the client
and server share the same password by default. See
https://opencode.ai/docs/server#authentication.

The SSE stream (`stream_events`) is intentionally NOT retried mid-stream by
this client — a dropped SSE connection should be re-established by the caller,
which owns the reconnect/backoff loop. Note: the Discord bot no longer uses
the SSE stream; it polls `GET /session/status` via `get_session_status` (see
`bot.events.poll_until_idle`) because the v2 SSE wire format proved fragile to
parse. `stream_events` is retained for other potential consumers.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from opencode_discord_bot.config import config


def _auth() -> tuple[str, str] | None:
    """Basic-auth tuple if OPENCODE_SERVER_PASSWORD is set, else None."""
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    if not password:
        return None
    username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    return (username, password)


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
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
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
        resp = await client.request(method, path, **kw)
        if resp.status_code >= 400:
            raise OpencodeError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

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

    async def send_message(
        self,
        sid: str,
        parts: list[dict],
        agent: str | None = None,
        model: str | None = None,
    ) -> dict:
        """POST /session/{id}/message — synchronous wait for full response.

        `parts` is the opencode message-parts array, e.g.
        ``[{"type": "text", "text": "..."}]``. `agent` selects an opencode agent
        (e.g. ``"plan"`` for plan mode). Returns ``{ info, parts }``.
        """
        body: dict = {"parts": parts}
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
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
        `bot.events.poll_until_idle`).
        """
        body: dict = {"parts": parts}
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
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
            "GET", "/event", headers={"Accept": "text/event-stream"}
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
