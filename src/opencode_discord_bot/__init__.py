"""Discord control bot for opencode (two-way gateway, run via `python -m opencode_discord_bot`).

Owns the `opencode serve` HTTP server lifecycle: `OpencodeBot.on_connect`
spawns the server as a subprocess on login and `OpencodeBot.close` stops it
on shutdown (see `opencode_serve.py`). The bot's `OpencodeClient` then talks
to that local server to control and monitor opencode remotely from Discord.
Runs as a standalone process; config comes from environment variables / a
`.env` file (see `config.py`).

Re-exports are lazy (`__getattr__`) so each leaf module is independently
importable for per-step verification, rather than `__init__.py` dragging in
every leaf (which would make `import opencode_discord_bot.opencode_client`
fail until every module exists).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["OpencodeBot", "run_bot"]


def __getattr__(name: str) -> Any:
    if name == "OpencodeBot":
        from opencode_discord_bot.commands import OpencodeBot

        return OpencodeBot
    if name == "run_bot":
        from opencode_discord_bot.__main__ import main as run_bot

        return run_bot
    raise AttributeError(f"module 'opencode_discord_bot' has no attribute {name!r}")


if TYPE_CHECKING:  # for type checkers that resolve `__getattr__`
    from opencode_discord_bot.commands import OpencodeBot
    from opencode_discord_bot.__main__ import main as run_bot
