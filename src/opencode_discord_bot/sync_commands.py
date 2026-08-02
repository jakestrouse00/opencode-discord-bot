"""One-off command sync — pushes slash commands to a Discord guild without starting the gateway or the opencode serve subprocess.

Usage (from repo root):
    python -m opencode_discord_bot.sync_commands
    python -m opencode_discord_bot.sync_commands --guild 1532565085422223441
    python -m opencode_discord_bot.sync_commands --guild 0  # global (takes ~1hr)

Pycord (`py-cord[voice]`) auto-syncs commands in `on_connect` (which fires
during `connect()`, not `login()`), so for a one-off sync we call
`bot.sync_commands()` manually after `login()`. `login()` does NOT call
`on_connect` (Pycord has no `setup_hook`), and `on_connect` doesn't fire
without the gateway loop — so `login()` + `sync_commands()` is the minimal
path to push commands without starting the gateway. The `opencode serve`
subprocess is disabled anyway as a safety net.

Safe to run repeatedly; idempotent. Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import discord

from opencode_discord_bot.commands import OpencodeBot
from opencode_discord_bot.config import config

_log = logging.getLogger("bot.sync_commands")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m opencode_discord_bot.sync_commands",
        description="Push opencode bot slash commands to a Discord guild (no gateway, no opencode serve).",
    )
    p.add_argument(
        "--guild",
        type=int,
        default=None,
        help="Override discord_bot_guild_id for this sync (defaults to settings).",
    )
    return p.parse_args(argv)


async def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    settings = config
    token = settings.discord_bot_token
    if not token:
        print(
            "ERROR: discord_bot_token is empty. Set DISCORD_BOT_TOKEN or fill in "
            "discord_bot_token in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    guild_id = args.guild if args.guild is not None else settings.discord_bot_guild_id
    if not guild_id:
        print(
            "ERROR: no guild id. Pass --guild <id> or set discord_bot_guild_id "
            "in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    bot = OpencodeBot()
    # Disable the serve subprocess for this one-off sync so on_connect (if it
    # fires) doesn't spawn it. Pycord's login() does NOT call on_connect (it
    # doesn't exist without the gateway), but this is a good safety net.
    bot._serve._enabled = False
    try:
        await bot.login(token)
        # Pycord's sync_commands pushes registered slash commands to Discord.
        # guild_ids=[guild_id] restricts the sync to the target guild;
        # check_guilds=[guild_id] ensures the guild's commands are checked.
        await bot.sync_commands(guild_ids=[guild_id], check_guilds=[guild_id])
        cmds = bot.application_commands
        _log.info("Synced %d command(s) to guild %d:", len(cmds), guild_id)
        for cmd in cmds:
            _log.info("  /%s — %s", cmd.name, cmd.description)
    except discord.DiscordException as e:
        _log.error("Sync failed: %r", e)
        sys.exit(1)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
