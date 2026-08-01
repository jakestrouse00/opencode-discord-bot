"""Entry point for `python -m bot` (also re-used by `bot/bot_start.py`).

Reads the bot token from `config.discord_bot_token` (env-overridable via
`DISCORD_BOT_TOKEN`), constructs `OpencodeBot`, and starts the gateway. Mirrors
the `asyncio.run(main())` pattern from `main.py:33`.

Supports optional CLI overrides (env/settings already cover everything; CLI is
a convenience for ad-hoc runs):
    --guild <id>      override discord_bot_guild_id for this run
    --server <url>    override opencode_server_url for this run

Run from the repo root in a separate terminal from `python main.py`:
    python -m bot
    python -m bot --guild 123456789 --server http://127.0.0.1:4096
    python bot/bot_start.py

Do NOT run without a token — it exits(1) cleanly with a clear message.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from opencode_discord_bot.config import config

_log = logging.getLogger("bot")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m bot",
        description="Discord control bot for opencode (two-way gateway).",
    )
    p.add_argument(
        "--guild",
        type=int,
        default=None,
        help="Override discord_bot_guild_id for this run.",
    )
    p.add_argument(
        "--server",
        type=str,
        default=None,
        help="Override opencode_server_url for this run.",
    )
    return p.parse_args(argv)


async def main() -> None:
    args = _parse_args()

    # env vars (DISCORD_BOT_TOKEN, etc.) already flow through pydantic-settings
    # into the `config` singleton at import. CLI overrides mutate the singleton
    # in place so every module that reads `config.<field>` sees the override.
    if args.guild is not None:
        config.discord_bot_guild_id = args.guild
    if args.server is not None:
        config.opencode_server_url = args.server

    token = config.discord_bot_token
    if not token:
        print(
            "ERROR: discord_bot_token is empty. Set the DISCORD_BOT_TOKEN env var "
            "or fill in discord_bot_token in bot/config.py / .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    from opencode_discord_bot.commands import OpencodeBot

    _log.info(
        "Starting opencode Discord bot (server=%s, guild=%s)",
        config.opencode_server_url,
        config.discord_bot_guild_id or "(global sync)",
    )

    bot = OpencodeBot()
    try:
        await bot.start(token)
    except KeyboardInterrupt:
        pass
    finally:
        # Always close so OpencodeBot.close() stops the opencode serve
        # subprocess (setup_hook started it). Without this, Ctrl-C before
        # the gateway is up would orphan the server.
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
