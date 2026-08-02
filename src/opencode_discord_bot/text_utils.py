"""Pure-stdlib text helpers shared by the Discord bot and the Comulytic bridge.

Lives in a Pycord-free module so the Comulytic bridge (which deliberately
avoids importing `commands.py` — that file imports `discord` at module top)
can reuse the same `_split_message` / `_extract_text` / `_final_assistant_text`
/ `_slugify_prompt` helpers without dragging Pycord into its happy path.

This module is the single source of truth; `commands.py`, `comulytic.py`,
and `bridge.py` all import from here.
"""

from __future__ import annotations

import re

# Discord message length cap (hard API limit). Long opencode responses must
# be split into chunks at most this long.
DISCORD_MSG_MAX = 2000

# Discord text-channel name length cap (hard API limit).
_CHANNEL_NAME_MAX = 100


def _split_message(text: str, limit: int = DISCORD_MSG_MAX) -> list[str]:
    """Split a long string into <=limit chunks, preferring code-block / newline boundaries.

    Order of preference: code-fence boundaries (```), double newlines, single
    newlines, then hard char splits. Each chunk is <= limit. Never returns an
    empty list (returns [""] for empty input).
    """
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        # try to split on a code-fence boundary near the limit
        cut = -1
        fence = remaining.rfind("\n```", 0, limit)
        if fence != -1:
            # include the closing fence line in this chunk
            cut = fence + 4
        else:
            para = remaining.rfind("\n\n", 0, limit)
            if para != -1:
                cut = para + 2
            else:
                nl = remaining.rfind("\n", 0, limit)
                if nl != -1:
                    cut = nl + 1
                else:
                    # last resort: hard split at limit
                    cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


def _extract_text(parts: list[dict]) -> str:
    """Concatenate all text parts from an opencode message's `parts` array.

    `list_messages` returns ``[{"info": Message, "parts": [Part]}, ...]`` where
    each `Part` is ``{"type": "text"|"image"|"tool", "text": ... | ...}``.
    Returns the joined text, or "" if no text parts.
    """
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            t = part.get("text")
            if t:
                out.append(t)
    return "\n".join(out) if out else ""


def _final_assistant_text(messages: list[dict]) -> str:
    """Extract the text of the last assistant message from list_messages output.

    `list_messages` returns ``[{"info": Message, "parts": [Part]}, ...]``. We
    want the last message whose `info.role == "assistant"` and whose parts
    contain text. Falls back to the last message with any text.
    """
    last_assistant: str | None = None
    last_any: str | None = None
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info") or {}
        parts = entry.get("parts") or []
        text = _extract_text(parts)
        if not text:
            continue
        last_any = text
        if info.get("role") == "assistant":
            last_assistant = text
    return last_assistant if last_assistant is not None else (last_any or "")


def _slugify_prompt(prompt: str, fallback: str) -> str:
    """Turn a prompt into a Discord channel name slug.

    Takes the first ~6 words, lowercases, collapses non-[a-z0-9-] runs to
    single hyphens, strips leading/trailing hyphens, and caps at
    `_CHANNEL_NAME_MAX` chars. Returns `fallback` if the prompt yields no
    usable slug (empty, all-symbols, etc.).
    """
    words = prompt.split()[:6]
    slug = "-".join(words).lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return fallback
    return slug[:_CHANNEL_NAME_MAX]
