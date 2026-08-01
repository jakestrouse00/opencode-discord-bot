"""Maps Discord channel id -> opencode session id, persisted to a JSON file.

One persistent opencode session per Discord channel. The map is loaded on
construction and saved after each new binding so a bot restart does not lose
the channel->session binding (opencode sessions survive on the server side,
so reattaching is just restoring the id mapping).

The persisted file (`bot/.sessions.json` by default) is gitignored — see
`bot/.gitignore`. It contains only session ids, no secrets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode_discord_bot.opencode_client import OpencodeClient

_DEFAULT_PATH = Path(".opencode-discord-bot-sessions.json")


class SessionRouter:
    """Channel id (int) -> opencode session id (str), persisted to JSON.

    `current` and `reset` are sync (pure local-map ops); `get_or_create` is
    async because it calls the async `OpencodeClient.create_session` on a
    cache miss. Callers run it from within the bot's event loop (slash-command
    handlers are async).
    """

    def __init__(self, persist_path: Path = _DEFAULT_PATH) -> None:
        self._path = persist_path
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._map = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(self._map, dict):
                    self._map = {}
            except (json.JSONDecodeError, OSError):
                self._map = {}
        else:
            self._map = {}

    def save(self) -> None:
        """Write the current map to disk (best-effort, never raises)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._map, indent=2), encoding="utf-8")
        except OSError:
            pass

    def current(self, channel_id: int) -> str | None:
        """Return the bound opencode session id for a channel, or None."""
        return self._map.get(str(channel_id))

    async def get_or_create(
        self,
        channel_id: int,
        client: "OpencodeClient",
        title: str | None = None,
    ) -> str:
        """Return the bound session id, creating + persisting a new one if none.

        The opencode session title defaults to ``f"discord-{channel_id}"`` so
        it's identifiable in the opencode session list. Always creates a fresh
        opencode session on first bind for a channel.
        """
        key = str(channel_id)
        if key in self._map:
            return self._map[key]
        session_title = title or f"discord-{channel_id}"
        session = await client.create_session(session_title)
        sid = session["id"]
        self._map[key] = sid
        self.save()
        return sid

    def reset(self, channel_id: int) -> None:
        """Drop the channel->session binding (does NOT delete the opencode session).

        Next `get_or_create` makes a fresh session. Use after `oc_new` or when
        the user wants to start over without losing the old opencode session
        history (which remains on the server until explicitly deleted).
        """
        self._map.pop(str(channel_id), None)
        self.save()

    def bind(self, channel_id: int, session_id: str) -> None:
        """Bind a channel to an already-created opencode session id, persisted.

        Unlike `get_or_create`, this does NOT create an opencode session — the
        caller has already created the session (and the Discord channel) and
        just needs the binding recorded. Used by `/oc` and `/oc_plan` after
        they create both the opencode session and a fresh Discord text channel
        for it.
        """
        self._map[str(channel_id)] = session_id
        self.save()
