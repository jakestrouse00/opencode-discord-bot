"""Lightweight LLM Discord channel-name slug generator for the control bot.

Mirrors opencode's hidden "title" agent pattern: a single short chat
completion on the smallest available cloud model produces a real slug from
the user's text, instead of the regex-only `_slugify_prompt`'s word-salad
truncation. The result is re-normalized through the same regex pipeline as
`_slugify_prompt` so the LLM output is always a valid Discord channel name.

Import-isolated from the heavy `core.agent` / `core.agents` chain on
purpose — the bot process is lightweight (it imports
`opencode_discord_bot.config` and `opencode_discord_bot.opencode_serve`
only). Pulling the agent registry in here would spin up MCP toolsets,
Langfuse instrumentation, and the full `@register_agent` decorator chain
on import. The slug generator uses raw `httpx` against the OpenAI-compatible
Ollama Cloud `/chat/completions` endpoint — the same endpoint
`core/agent.py:87` builds `OllamaProvider` against, just called directly.

`generate_slug` NEVER raises. On any error (HTTP error, timeout, unexpected
JSON shape, empty result, network failure) it returns the supplied
`fallback` so channel creation is never blocked.
"""

from __future__ import annotations

import logging
import re

import httpx

from opencode_discord_bot.config import config

_log = logging.getLogger("bot.slug")

# Discord text-channel name length cap (hard API limit; matches
# `text_utils._CHANNEL_NAME_MAX`). Duplicated here rather than imported
# to keep this module dependency-light (importing from `opencode_discord_bot.commands`
# would pull Pycord into the slug path unnecessarily).
_CHANNEL_NAME_MAX = 100

# Module-level lazily-initialized client. Every `/oc*` command triggers a
# slug call (fire-and-forget), and a fresh `httpx.AsyncClient` per call would
# re-do the TLS handshake + connection pool setup each time. Reusing one
# client amortizes that and enables HTTP/2 connection reuse to Ollama Cloud.
_slug_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """Return the module-level slug client, constructing it lazily on first use.

    Per-request `timeout` is still honored via `client.post(..., timeout=...)`
    so a runtime change to `config.slug_timeout_seconds` takes effect on the
    next call without rebuilding the client.
    """
    global _slug_client
    if _slug_client is None:
        _slug_client = httpx.AsyncClient(timeout=config.slug_timeout_seconds)
    return _slug_client


async def aclose_slug_client() -> None:
    """Close the module-level slug client on bot shutdown (best-effort)."""
    global _slug_client
    if _slug_client is not None:
        try:
            await _slug_client.aclose()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass
        _slug_client = None


# Mirrors opencode's `packages/opencode/src/agent/prompt/title.txt` shape:
# short instruction, lowercase + hyphen-separated, output ONLY the slug.
SLUG_PROMPT = (
    "You generate a short Discord channel name slug from the user's message. "
    "Output ONLY the slug — lowercase, hyphen-separated, at most 50 "
    "characters, no explanation, no quotes. Focus on the main topic. "
    "Example: 'debug 500 errors in production' -> 'debug-500-errors'."
)


def _normalize(raw: str) -> str:
    """Apply the same regex normalization as `_slugify_prompt`.

    Lowercase, collapse non-[a-z0-9-] runs to single hyphens, strip
    leading/trailing hyphens, cap at `_CHANNEL_NAME_MAX` chars.
    """
    slug = raw.lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:_CHANNEL_NAME_MAX]


async def generate_slug(prompt: str, fallback: str) -> str:
    """Generate a Discord channel-name slug from `prompt` via one LLM call.

    Makes a single POST to Ollama Cloud's OpenAI-compatible
    `/chat/completions` endpoint with the bearer auth key, extracts the
    assistant message content, regex-normalizes it, and returns the slug. On
    ANY exception (HTTP error, timeout, KeyError/IndexError on an unexpected
    response shape, empty/whitespace result) returns `fallback` unchanged.
    Never raises.
    """
    if not prompt or not prompt.strip():
        return fallback
    try:
        client = _client()
        resp = await client.post(
            f"{config.ollama_api_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.ollama_auth_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.slug_model,
                "messages": [
                    {"role": "system", "content": SLUG_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 512,
                "temperature": 0.3,
                "stream": False,
            },
            timeout=config.slug_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 — slug is best-effort, never raise
        _log.warning("slug LLM call failed; using fallback %r: %r", fallback, e)
        return fallback

    if not content or not content.strip():
        return fallback
    slug = _normalize(content.strip().strip("'\""))
    if not slug:
        return fallback
    return slug
