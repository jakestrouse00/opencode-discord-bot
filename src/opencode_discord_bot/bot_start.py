"""Script entry point for the opencode Discord control bot.

Run from the repo root:
    python -m opencode_discord_bot.bot_start
    python -m opencode_discord_bot.bot_start --guild 123456789 --server http://127.0.0.1:4096

This is a thin wrapper over `opencode_discord_bot.__main__.main` (the
`python -m opencode_discord_bot` path) so the two entry points stay in sync —
the real argument parsing, settings overrides, logging, and OpencodeBot
lifecycle (start opencode serve in `on_connect`, stop it in `close`) all
live there. Kept here so the bot can be launched as a plain script without
the `-m` flag.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# When run as `python -m opencode_discord_bot.bot_start`, the package is
# already importable; when run as a bare script, sys.path[0] is the
# `opencode_discord_bot/` directory, so the package import can't resolve.
# Insert the repo root (parent of this file's package dir) so the package
# imports work the same as `python -m opencode_discord_bot` (which sets
# sys.path[0] to the repo root / `src/`).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from opencode_discord_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
