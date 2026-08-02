"""Idempotent `.env` file updater for the `/oc_setup` slash command.

Writes discovered Discord guild IDs (category id, channel ids, guild id)
back to the bot's `.env` file so they persist across restarts. pydantic-settings
reads `.env` at `BotConfig` construction (see `config.py`), but does NOT
round-trip writes back — there's no `python-dotenv` write API in use here, and
adding one as a hard dep just for `/oc_setup` would bloat the install. This
module is a pure-stdlib line-by-line updater:

  - For each `KEY=value` line whose `KEY` is in `updates`, replace the value
    in place (preserving the `KEY=` prefix and the line's position).
  - For keys in `updates` that are NOT already in the file, append them at
    the end (one `KEY=value` line per missing key, no comment).
  - Comments, blank lines, and the original order of existing lines are
    preserved. `KEY=value` lines whose key is not in `updates` are left
    untouched.
  - The write is atomic: the file is rewritten to `<path>.tmp` and then
    `os.replace`'d into place, so a crash mid-write can't leave a half-written
    `.env` (which would break the bot's next launch).

Quoting: Discord IDs are plain integers and JSON lists are ASCII, so no
quoting/escaping is needed. Values are written verbatim. If a value ever
needs shell-style quoting in `.env`, the caller is responsible for including
the quotes in the string passed to `update_env_file` (pydantic-settings
strips surrounding single/double quotes on read, so round-tripping works).

Only used by the `/oc_setup` slash command's callback
(`OpencodeBot._run_setup` in `opencode_discord_bot/commands.py`). Kept in
its own module (rather than inlined in `commands.py`) so it's independently
importable for verification (`python -c "from
opencode_discord_bot.env_writer import update_env_file; print('ok')"`) and
so the bot's command module doesn't grow a second unrelated concern.
"""

from __future__ import annotations

import os
from pathlib import Path


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Apply `updates` (KEY -> value) to the `.env` file at `path`.

    Idempotent and atomic. Missing keys are appended at the end; present
    keys have their values replaced in place. The file is created if it
    doesn't exist (the common case on a fresh install with only `.env.example`
    copied to `.env` — but `/oc_setup` may also be the first thing to write
    a real `.env`).

    `path` should be the absolute path to the `.env` file (typically
    `Path.cwd() / ".env"`, matching `BotConfig.model_config`'s
    `env_file=".env"` which is cwd-relative).
    """
    path = Path(path)
    # Read existing lines if the file exists; else start from an empty file.
    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
    else:
        text = ""
        lines = []

    remaining = dict(updates)  # keys still to be written (copied so we can pop)
    out: list[str] = []

    for line in lines:
        # Skip blank lines and comment-only lines verbatim (no KEY= to match).
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        # Match `KEY=value` or `KEY = value` (pydantic-settings tolerates both,
        # but the canonical form is no spaces around `=`). Only split on the
        # first `=` so values containing `=` are preserved.
        if "=" in stripped:
            key, _, _rest = stripped.partition("=")
            key = key.strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        # Not a key we're updating — pass through unchanged.
        out.append(line)

    # Append any keys that weren't already in the file. Add a blank line
    # separator if the file had content and didn't already end with one, so
    # the appended keys don't stick to the last existing line.
    if remaining:
        if out and out[-1].strip() != "":
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    new_text = "\n".join(out)
    # Preserve a trailing newline if the original had one (and the file was
    # non-empty), so the rewrite doesn't silently drop the final newline.
    if text.endswith("\n") and new_text:
        new_text += "\n"
    elif new_text and not new_text.endswith("\n"):
        # Ensure the file ends with a newline (POSIX text-file convention;
        # also keeps future appends from concatenating onto the last line).
        new_text += "\n"

    # Atomic write: write to a temp file in the same directory and os.replace
    # into place. Same-dir temp is required so `os.replace` stays on the
    # same filesystem (an atomic rename across filesystems would fall back to
    # copy+unlink, which is NOT atomic on a crash).
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
