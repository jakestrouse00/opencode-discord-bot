"""Raw Discord REST API client (no Pycord, no gateway connection).

Used by the Comulytic bridge to create Discord channels, post messages, and
edit them via plain HTTPS calls to Discord's REST v10 API — without dragging
Pycord into the bridge process or competing with the main bot's gateway
connection (Discord allows ONE gateway connection per bot token; a second
connection would kick the first into a reconnect loop).

The main Discord bot (`commands.py`) uses Pycord's `guild.create_text_channel`
/ `channel.send` / `message.edit` / `channel.edit` — all gateway-backed. This
module wraps the SAME underlying REST endpoints Pycord uses, but via a bare
`httpx.AsyncClient` with `Authorization: Bot {token}`. Safe to run alongside
the main bot: REST rate limits are per-token (shared), but channel creation
+ a few messages per recording is low-volume and well within limits.

References:
  - Create channel:  POST /guilds/{guild_id}/channels
    https://discord.com/developers/docs/resources/guild#create-guild-channel
  - Create message:  POST /channels/{channel_id}/messages
    https://discord.com/developers/docs/resources/channel#create-message
  - Edit message:    PATCH /channels/{channel_id}/messages/{message_id}
    https://discord.com/developers/docs/resources/channel#edit-message
  - Edit channel:    PATCH /channels/{channel_id}
    https://discord.com/developers/docs/resources/channel#modify-channel
  - List messages:   GET /channels/{channel_id}/messages
    https://discord.com/developers/docs/resources/channel#get-channel-messages

Auth header on every request: ``Authorization: Bot {token}``. Audit-log
reasons go in the ``X-Audit-Log-Reason`` header (channel create/edit only).

Never imports Pycord — safe to import from the bridge's happy path.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

_log = logging.getLogger("comulytic.discord_rest")

# Discord REST API base. v10 is the current documented version.
_API_BASE = "https://discord.com/api/v10"

# Per-request timeout. Discord REST is normally fast; channel create / message
# post round-trip is ~100-300ms. 10s is a defensive ceiling.
_REQUEST_TIMEOUT = 10.0

# Retry budget for 429 (rate limit) responses. We honor Retry-After once; if the
# retry also 429s, we raise (the caller decides whether to skip or back off).
_MAX_429_RETRIES = 1


class DiscordRestError(RuntimeError):
    """Raised when Discord REST returns a non-success status (after retries)."""


class DiscordRest:
    """Async httpx wrapper over Discord REST v10 for the Comulytic bridge.

    Construct with the bot token (``config.discord_bot_token``). All methods
    return the parsed JSON response body (a dict) on success and raise
    ``DiscordRestError`` on non-2xx (after honoring one 429 retry). The
    ``Authorization: Bot {token}`` header is set once on the client and sent
    on every request.
    """

    def __init__(self, token: str, *, base_url: str = _API_BASE) -> None:
        if not token:
            raise DiscordRestError("Discord bot token is empty")
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "opencode-discord-bot bridge (raw REST)",
            },
            timeout=_REQUEST_TIMEOUT,
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    # --- internal request helper -------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        reason: str = "",
        _retry: int = 0,
    ) -> dict:
        url = f"{self._base}{path}"
        headers: dict[str, str] = {}
        if reason:
            # Discord audit-log header (URL-encoded by httpx); max 512 chars.
            headers["X-Audit-Log-Reason"] = reason[:512]
        resp = await self._client.request(
            method, url, json=json, params=params, headers=headers
        )
        if resp.status_code == 429 and _retry < _MAX_429_RETRIES:
            # Honor Retry-After (seconds; Discord returns a float string for
            # global rate limits, an int for per-route). Sleep once, retry.
            retry_after = float(resp.headers.get("Retry-After", "1"))
            _log.warning(
                "Discord 429 on %s %s — retrying after %.2fs", method, path, retry_after
            )
            import asyncio

            await asyncio.sleep(retry_after)
            return await self._request(
                method, path, json=json, params=params, reason=reason, _retry=_retry + 1
            )
        if not (200 <= resp.status_code < 300):
            raise DiscordRestError(
                f"Discord {method} {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        # 204 No Content (rare for these endpoints, but be defensive).
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # --- channel operations ------------------------------------------------

    async def create_text_channel(
        self,
        guild_id: int,
        name: str,
        *,
        parent_id: int = 0,
        topic: str = "",
        reason: str = "",
    ) -> dict:
        """Create a text channel under an optional category.

        Returns the new channel object (including ``id``). Discord validates
        the name (1-100 chars, lowercase-preferred) and parent_id (must be a
        CategoryChannel in the guild).
        """
        body: dict[str, Any] = {"name": name, "type": 0}  # 0 = text channel
        if parent_id:
            body["parent_id"] = parent_id
        if topic:
            body["topic"] = topic
        return await self._request(
            "POST", f"/guilds/{guild_id}/channels", json=body, reason=reason
        )

    async def edit_channel(
        self,
        channel_id: int,
        name: str,
        *,
        reason: str = "",
    ) -> dict:
        """Rename a channel (PATCH /channels/{id}).

        Used by the fire-and-forget LLM slug upgrade (mirrors
        `commands.py:_rename_when_slug_ready` calling `TextChannel.edit`).
        """
        return await self._request(
            "PATCH", f"/channels/{channel_id}", json={"name": name}, reason=reason
        )

    # --- message operations ------------------------------------------------

    async def create_message(self, channel_id: int, content: str) -> dict:
        """Post a message to a channel (POST /channels/{id}/messages).

        Returns the message object (including ``id``). Discord's content cap
        is 2000 chars; callers MUST chunk via `_split_message` first.
        """
        return await self._request(
            "POST", f"/channels/{channel_id}/messages", json={"content": content}
        )

    async def edit_message(
        self, channel_id: int, message_id: int, content: str
    ) -> dict:
        """Edit a message (PATCH /channels/{id}/messages/{msg_id}).

        Used for the throttled progress-edit loop (mirrors
        `commands.py:_drive_session` editing `progress_msg`).
        """
        return await self._request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json={"content": content},
        )

    async def list_messages(
        self,
        channel_id: int,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        """List messages after a snowflake id (GET /channels/{id}/messages).

        Returns the message list (newest-first per Discord). Used by the
        question-reply polling loop to find the user's plain-text reply after
        a question is posted. ``after`` is the snowflake id to start after;
        0 = most recent messages.
        """
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        result = await self._request(
            "GET", f"/channels/{channel_id}/messages", params=params
        )
        return result if isinstance(result, list) else []
