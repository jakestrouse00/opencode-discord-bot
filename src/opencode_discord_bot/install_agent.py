"""Install the bundled `oc-assistant` opencode agent (Bobby) into a target project.

Usage:
    python -m opencode_discord_bot.install_agent [--dest <path>] [--force]

Copies the bundled `oc-assistant.md` (shipped inside this package at
`opencode_discord_bot/agent/oc-assistant.md`) into the target project's
`.opencode/agent/` directory so the opencode server (spawned by the Discord
bot against that project) can discover and invoke it via `agent="oc-assistant"`.

The bundled agent is a fully generic, self-contained assistant subagent
("Bobby") — it reads the target project's own `AGENTS.md` (if present),
writes only under `.opencode/assistant/{plans,notes,thoughts}/`, and is what
the bot's `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message trigger, and
Comulytic-bridge paths route to. Without it installed in the project the
bot's `opencode serve` is running against, those paths will 404 / fail
agent resolution.

Defaults:
    --dest  the current working directory (`Path.cwd()`). The agent lands at
            `<dest>/.opencode/agent/oc-assistant.md`. Override when your `.env`
            lives in a subdirectory but your project root (and `.opencode/`)
            lives elsewhere — mirrors the `OPENCODE_SERVE_CWD` setting.
    --force overwrite an existing `oc-assistant.md` without prompting. By
            default the install prompts before overwriting (and refuses on
            any non-yes answer), so a project that has its own customized
            oc-assistant isn't silently clobbered.

Reads the bundled file via `importlib.resources` so it works the same whether
the package was installed via `pip install -e .`, `pip install git+...`, or a
built wheel — the file is shipped inside the package.

Exit codes:
    0  installed (or already present + --force not needed / refused overwrite)
    1  refused overwrite, --dest doesn't exist, or the bundled file is missing
      (the latter indicates a broken install — the file should ship with the
      package).

Run from the target project's root directory (or pass `--dest <project root>`)
for the simplest workflow. See `SETUP_GUIDE.md` "Install the oc-assistant
agent" section.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from importlib.resources import files as _resource_files
from pathlib import Path

# The bundled agent file ships inside the package at this relative path.
_AGENT_RESOURCE = "agent/oc-assistant.md"
# Where it lands in the target project (relative to --dest).
_AGENT_DEST_REL = Path(".opencode") / "agent" / "oc-assistant.md"
# Legacy filename from before the rename — left in place by the installer if
# present, but flagged as stale so the user knows it's safe to delete.
_STALE_LEGACY_DEST_REL = Path(".opencode") / "agent" / "plan-author.md"


def _bundled_text() -> str:
    """Read the bundled oc-assistant.md text from package resources.

    `importlib.resources.files("opencode_discord_bot")` returns a Traversable
    rooted at the package's install location (works for editable installs,
    wheels, and zipped distributions). The bundled file lives at
    `opencode_discord_bot/agent/oc-assistant.md` inside the package.
    """
    root = _resource_files("opencode_discord_bot")
    resource = root.joinpath(_AGENT_RESOURCE)
    try:
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise SystemExit(
            f"ERROR: bundled agent file not found inside the package at "
            f"{_AGENT_RESOURCE!r}. This indicates a broken install — the file "
            f"should ship with the opencode_discord_bot package. Reinstall via "
            f"`pip install --force-reinstall opencode-discord-bot` (or "
            f"`pip install -e .` from a local clone). Underlying error: {exc}"
        ) from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m opencode_discord_bot.install_agent",
        description=(
            "Install the bundled oc-assistant opencode agent (Bobby) into a "
            "target project's .opencode/agent/ directory so the Discord bot "
            "can route oc-assistant prompts to it."
        ),
    )
    p.add_argument(
        "--dest",
        type=Path,
        default=Path.cwd(),
        help=(
            "Target project root (defaults to the current working directory). "
            "The agent lands at <dest>/.opencode/agent/oc-assistant.md."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing oc-assistant.md without prompting.",
    )
    return p.parse_args(argv)


def install(dest: Path, *, force: bool = False) -> int:
    """Copy the bundled oc-assistant.md into `<dest>/.opencode/agent/`.

    Returns the process exit code (0 on success, 1 on refusal/error). Public
    function so other entry points (tests, future install scripts) can call
    it without going through the CLI.
    """
    dest = dest.expanduser().resolve()
    if not dest.exists():
        print(f"ERROR: --dest {dest} does not exist.", file=sys.stderr)
        return 1
    if not dest.is_dir():
        print(f"ERROR: --dest {dest} is not a directory.", file=sys.stderr)
        return 1

    target = dest / _AGENT_DEST_REL
    if target.exists() and not force:
        answer = (
            input(
                f"{target} already exists. Overwrite with the bundled generic "
                f"version? [y/N] "
            )
            .strip()
            .lower()
        )
        if answer not in {"y", "yes"}:
            print(
                "Keeping the existing file. Pass --force to overwrite without "
                "prompting."
            )
            return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    text = _bundled_text()
    target.write_text(text, encoding="utf-8")
    print(f"Installed oc-assistant agent (Bobby) to {target}")
    print(
        "The Discord bot's /oc_plan, /oc_voice, /oc_talk, voice-message, and "
        "Comulytic-bridge paths will now route to this agent when opencode "
        "serve runs against this project."
    )
    # Stale-file notice: a prior install_agent run wrote plan-author.md here;
    # the bot no longer routes to it. Don't auto-delete — the user may have a
    # customized copy they want to inspect before removing.
    legacy = dest / _STALE_LEGACY_DEST_REL
    if legacy.exists() and legacy.resolve() != target.resolve():
        print(
            "Note: a stale .opencode/agent/plan-author.md is still present; "
            "the bot no longer routes to it — safe to delete."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return install(args.dest, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())