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
    each `Part` is ``{"type": "text"|"reasoning"|"image"|"tool", "text": ... | ...}``.
    ``text`` parts are preferred; ``reasoning`` parts are a fallback so a
    reasoning-only assistant turn (which some models emit in place of a
    final ``text`` part) still yields content. Returns the joined text
    (text parts first, then reasoning parts), or "" if neither is present.
    """
    text_out: list[str] = []
    reasoning_out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            t = part.get("text")
            if t:
                text_out.append(t)
        elif ptype == "reasoning":
            # Reasoning parts carry a `text` field per
            # packages/schema/src/v1/session.ts (~line 118-128) and are valid
            # assistant content. Some models (notably with `variant: medium`)
            # emit the summary as reasoning and end the turn after a `write`
            # tool call without a final `text` part; without this fallback
            # the assistant message would yield "" and the bridge would
            # incorrectly fall back to the user prompt.
            t = part.get("text")
            if t:
                reasoning_out.append(t)
    out = text_out + reasoning_out
    return "\n".join(out) if out else ""


def _final_assistant_text(messages: list[dict]) -> str:
    """Extract the text of the last assistant message from list_messages output.

    `list_messages` returns ``[{"info": Message, "parts": [Part]}, ...]``. We
    want the last message whose `info.role == "assistant"` and whose parts
    contain text or reasoning. Returns "" if no assistant message with
    text/reasoning parts exists — **does NOT fall back to non-assistant
    messages**, because the only non-assistant text in a driven session is
    the user prompt (which, on the Comulytic bridge, is the transcript
    prefixed with ``[DISCORD_BOT]``/``[COMULYTIC_BRIDGE]`` tags). Falling
    back to the user prompt would echo the transcript as if it were the
    agent's reply — the exact bug this function previously had.
    """
    last_assistant: str | None = None
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info") or {}
        parts = entry.get("parts") or []
        text = _extract_text(parts)
        if not text:
            continue
        if info.get("role") == "assistant":
            last_assistant = text
    return last_assistant if last_assistant is not None else ""


# Internal directive tags the Comulytic bridge prepends to the prompt sent
# to oc-assistant (see bridge.py:route_to_assistant). They never appear in an
# agent reply. Used by `_looks_like_prompt` to detect a regression where the
# prompt leaks through as the "response".
_DIRECTIVE_TAGS = ("[DISCORD_BOT]", "[COMULYTIC_BRIDGE]")


def _looks_like_prompt(text: str) -> bool:
    """Heuristic: does ``text`` look like the user prompt rather than an
    agent reply?

    The Comulytic bridge prepends ``[DISCORD_BOT]`` and ``[COMULYTIC_BRIDGE]``
    directive lines to the prompt it sends to oc-assistant
    (bridge.py:route_to_assistant, ~line 1003-1005). Those tags are stripped
    by the agent before processing and never appear in an agent's output.
    If the extracted "final assistant text" contains either tag, it is
    almost certainly the user prompt that leaked through (e.g. via a
    fallback to a non-assistant message, or a malformed message list) — not
    a real reply. Used by the bridge as a belt-and-suspenders regression
    guard so a leak is caught and logged instead of posted to the Discord
    channel as the "response".
    """
    if not text:
        return False
    return any(tag in text for tag in _DIRECTIVE_TAGS)


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
