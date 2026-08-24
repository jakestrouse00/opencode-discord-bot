"""Comulytic bridge — poll Comulytic's cloud API for new recordings and route
their transcripts to opencode's `oc-assistant` agent.

Entry point: ``python -m opencode_discord_bot.bridge`` (or the
``comulytic-bridge`` console script). The bridge is a long-running asyncio
loop, NOT an HTTP server — it polls Comulytic's cloud API directly and uses
the existing `opencode serve` REST surface (via `OpencodeClient`) to drive
`oc-assistant`.

Happy path (local Whisper only — Comulytic's cloud ASR is NOT used):
  1. Poll `/note/paging` (cheap `pageSize:1` change-detect; full enumerate
     only when `total` changes).
  2. For each new audio-delivered recording, download the audio (Path B
     primary, Path A fallback) and transcribe LOCALLY via
     `voice.transcribe_audio` — the SAME pipeline `/oc_talk` uses
     (`extract_audio_to_wav` + `transcribe_audio`). `transcribe_audio`
     dispatches on `voice_stt_provider` (default `"local"` = faster-whisper
     CTranslate2, in-process; `"openai"` = cloud Whisper API; `"auto"` =
     local first, cloud fallback). By default transcription is fully local,
     private, and consistent with `/oc_talk`.
  3. Route the transcript to `oc-assistant` via `send_prompt_async`.

Discord surface (mirrors `/oc_talk`): when `discord_bot_token` +
`discord_bot_guild_id` are set, the bridge creates a Discord text channel
under `discord_bot_session_category_id`, posts the transcript there, fires
an LLM slug rename, sends the prompt to oc-assistant, surfaces any
clarifying questions as plain-text prompts in the channel (polling for the
user's reply), and posts the final response. Uses raw Discord REST via
``DiscordRest`` (no Pycord, no gateway connection — safe to run alongside
the main bot, which owns the gateway session). When Discord isn't
configured, falls back to log-only (route to oc-assistant, LOG the response).

`voice.py` (imported for every recording) pulls Pycord in at module top —
when the bridge runs in-process with the bot (the auto-spawn path) Pycord is
already loaded; the standalone `comulytic-bridge` console script loads it on
the first recording (Pycord is a hard dep, so this is fine).

Bootstrap-seen: the first poll cycle marks all currently-existing recordings
as seen WITHOUT processing them — only recordings created AFTER bridge
start get routed (no one-time backlog flood).

MASTER ENABLE: `comulytic_enabled` (default `False`). Set `COMULYTIC_ENABLED=true`
in `.env` (or the env var) to activate the bridge. When False, `main()`
exits immediately with a clear message. Even when True, the bridge still
requires `comulytic_jwt` to be set — if empty it exits with a different
clear message. The Discord surface additionally requires `DISCORD_BOT_TOKEN`
and `DISCORD_BOT_GUILD_ID` to be set; otherwise it degrades to log-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from opencode_discord_bot.bridge_questions import poll_pending_requests_rest
from opencode_discord_bot.bridge_state import clear_active, mark_active
from opencode_discord_bot.comulytic import (
    AudioUrlExpiredError,
    ComulyticClient,
    ComulyticError,
    is_audio_delivered,
    jwt_expiry,
)
from opencode_discord_bot.config import config
from opencode_discord_bot.discord_rest import DiscordRest, DiscordRestError
from opencode_discord_bot.events import poll_until_idle
from opencode_discord_bot.opencode_client import OpencodeClient, OpencodeError
from opencode_discord_bot.opencode_serve import OpencodeServe
from opencode_discord_bot.session_router import SessionRouter
from opencode_discord_bot.slug import aclose_slug_client, generate_slug
from opencode_discord_bot.text_utils import (
    _final_assistant_text,
    _looks_like_prompt,
    _slugify_prompt,
    _split_message,
)

_log = logging.getLogger("comulytic.bridge")

# JSON key tracking whether the seen-set has been bootstrapped. The first poll
# cycle marks all current noteIds as seen WITHOUT processing them so a one-time
# backlog flood on first run is avoided.
_BOOTSTRAP_KEY = "__bootstrapped__"

# How long to wait for the oc-assistant session to finish (poll_until_idle
# timeout). A long plan can take a while; 10 minutes is the default.
_ASSISTANT_TIMEOUT = 600.0

# Persistence file for the bridge's SessionRouter (channel id -> opencode
# session id). SEPARATE from the main bot's `.opencode-discord-bot-sessions.json`
# so the two processes don't clobber each other's writes (the main bot owns
# its channels; the bridge owns its channels). Gitignored — runtime state.
# The main bot (`commands.py`) mirrors this filename in `_BRIDGE_SESSIONS_FILE`
# (kept as a string constant there rather than imported here to avoid
# pulling the bridge's full import surface into the main bot's module top)
# so `OpencodeBot` can READ this file and `reset` entries on `/oc_new` /
# `/oc_cleanup` — keep the two constants in sync if you rename this file.
_BRIDGE_SESSIONS_FILE = ".opencode-discord-bridge-sessions.json"

# Throttle for editing the "Working on session…" progress message in the
# Discord channel (mirrors commands.py:PROGRESS_EDIT_MIN_INTERVAL = 2.0).
# Discord rate-limits message edits (~5 edits/5s per channel); 2s is safe.
_PROGRESS_EDIT_MIN_INTERVAL = 2.0

# How many times to re-fetch list_messages when no assistant text is found
# before giving up, and how long to sleep between attempts. Closes the
# timing race where poll_until_idle returns "idle" before the assistant
# message's text part is persisted (the fire-and-forget prompt_async gap
# that events.py guards against but can't fully close). 3 attempts × 2s is
# enough for the common "text part lands a second or two after idle" case
# without blocking the channel for long on a genuinely-text-less turn.
_FINAL_TEXT_RETRY_ATTEMPTS = 3
_FINAL_TEXT_RETRY_SLEEP = 2.0


# ---------------------------------------------------------------------------
# Seen-set persistence
# ---------------------------------------------------------------------------


def _load_seen(path: str) -> tuple[set[str], bool]:
    """Read the seen-set JSON from `path`. Returns `(seen_set, bootstrapped)`.

    If the file is missing or empty, returns `(set(), False)` — meaning the
    first poll must bootstrap (mark all current noteIds as seen WITHOUT
    processing them). On any JSON error, log + return `(set(), False)`.
    """
    p = Path(path)
    try:
        if not p.exists() or p.stat().st_size == 0:
            return set(), False
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return set(), False
        seen_list = data.get("seen") or []
        seen = {str(x) for x in seen_list if isinstance(x, (str, int, float))}
        bootstrapped = bool(data.get(_BOOTSTRAP_KEY, False))
        return seen, bootstrapped
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("seen-set file %s unreadable (%s) — re-bootstrapping", path, exc)
        return set(), False


def _save_seen(path: str, seen: set[str], bootstrapped: bool) -> None:
    """Write the seen-set + bootstrap flag to `path` as JSON.

    Called after each poll cycle and on shutdown. Runtime state, not source —
    the file is gitignored.
    """
    p = Path(path)
    try:
        p.write_text(
            json.dumps(
                {
                    "seen": sorted(seen),
                    _BOOTSTRAP_KEY: bootstrapped,
                },
                indent=0,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.error("failed to persist seen-set to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for `python -m opencode_discord_bot.bridge` / the
    `comulytic-bridge` console script.

    Mirrors `__main__.py:~72` logging setup. Validates `comulytic_enabled`
    (master enable) and `comulytic_jwt` (the captured access JWT) with clear
    error messages. Decodes the JWT expiry and warns if it's close.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    # --- MASTER ENABLE GATE (the .env disable mechanism) ---
    if not config.comulytic_enabled:
        _log.error(
            "comulytic_enabled is False — the Comulytic bridge is disabled. "
            "Set COMULYTIC_ENABLED=true in your .env (or the env var) to "
            "activate polling + oc-assistant routing. Exiting."
        )
        return

    # --- JWT gate ---
    if not config.comulytic_jwt:
        _log.error(
            "comulytic_jwt is empty. Capture a JWT from a login at "
            "web.comulytic.ai (see SETUP_GUIDE.md 'Comulytic bridge' section) "
            "and set COMULYTIC_JWT in your .env. Exiting."
        )
        return

    # --- JWT expiry warning ---
    try:
        exp = jwt_expiry(config.comulytic_jwt)
        now_utc = _now_utc()
        days_left = (exp - now_utc).total_seconds() / 86400.0
        if days_left <= 0:
            _log.error(
                "comulytic_jwt has EXPIRED (exp=%s, now=%s). All API calls "
                "will 401. Re-capture a JWT from a login at web.comulytic.ai "
                "and update COMULYTIC_JWT in your .env. Exiting.",
                exp.isoformat(),
                now_utc.isoformat(),
            )
            return
        if days_left <= config.comulytic_relogin_warn_days:
            _log.warning(
                "comulytic_jwt expires in %.1f day(s) (exp=%s). Re-login at "
                "web.comulytic.ai and update COMULYTIC_JWT before then. "
                "(The 365-day refresh token exists but the refresh *call* is "
                "a capture gap — the bridge treats the access JWT as "
                "non-refreshable until re-captured.)",
                days_left,
                exp.isoformat(),
            )
        else:
            _log.info(
                "comulytic_jwt expires %s (%.0f days remaining)",
                exp.isoformat(),
                days_left,
            )
    except ComulyticError as exc:
        _log.error("comulytic_jwt is malformed: %s. Exiting.", exc)
        return

    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        _log.info("bridge interrupted — shutting down")


# ---------------------------------------------------------------------------
# Async lifecycle
# ---------------------------------------------------------------------------


async def run_bridge() -> None:
    """The async lifecycle: construct clients, probe openapi visibility,
    load seen-set, enter the poll loop, and tear down on shutdown.

    Wrap in try/finally so the seen-set is always persisted + clients always
    closed, even on an uncaught exception.
    """
    # Seed OPENCODE_SERVER_PASSWORD into os.environ so OpencodeClient._auth()
    # picks it up (it reads from env at request time, NOT from BotConfig —
    # mirrors commands.py:on_connect at ~:492). `setdefault` so an env
    # override wins over the config default.
    if config.opencode_server_password:
        os.environ.setdefault(
            "OPENCODE_SERVER_PASSWORD", config.opencode_server_password
        )

    serve = OpencodeServe()
    opencode = OpencodeClient()
    comulytic = ComulyticClient(
        base_url=config.comulytic_api_base,
        jwt=config.comulytic_jwt,
        user_agent=config.comulytic_user_agent,
        web_base=config.comulytic_web_base,
        refresh_token=config.comulytic_refresh_token,
    )

    # Discord REST client for channel creation + message posting (raw httpx,
    # no Pycord, no gateway — safe to run alongside the main bot). Constructed
    # unconditionally; `route_to_assistant` gates on the token being non-empty
    # and gracefully degrades to the log-only path if Discord isn't configured.
    rest: DiscordRest | None = None
    if config.discord_bot_token:
        try:
            rest = DiscordRest(config.discord_bot_token)
        except DiscordRestError as exc:
            _log.warning("DiscordRest init failed (%s) — bridge will log-only", exc)
            rest = None
    else:
        _log.warning(
            "discord_bot_token is empty — bridge will route to oc-assistant and "
            "LOG the response only (no Discord channel will be created). Set "
            "DISCORD_BOT_TOKEN to enable the /oc_talk-style Discord surface."
        )

    # Bridge-owned SessionRouter (separate persistence file from the main bot
    # so the two processes don't clobber each other). Only used when rest is
    # active; constructed unconditionally so `route_to_assistant` can decide
    # at call time whether to bind.
    router = SessionRouter(Path(_BRIDGE_SESSIONS_FILE))

    # Start opencode serve (reuses a running one via _probe_healthy at
    # opencode_serve.py:~283, so no port conflict if the Discord bot is
    # running simultaneously). Run in a thread because start() is blocking.
    serve_ok = await asyncio.to_thread(serve.start)
    if not serve_ok:
        _log.warning(
            "opencode serve did not become healthy — oc-assistant routing will "
            "fail. Is opencode on PATH? (see SETUP_GUIDE.md). Continuing; the "
            "bridge will retry on each poll cycle."
        )

    seen: set[str] = set()
    bootstrapped = False
    try:
        # Step 4: openapi push-alternative probe (read-only). 2/3 HAR batches
        # (FNOUHEA + AEIYDDL) confirm the /api/openapi/v1/ surface exists in
        # the client bundle and is account-gated via
        # `GET /api/openapi/v1/api-keys/developer-tab/visibility`. If visible,
        # push mode is a real follow-up (inbound FastAPI endpoint + HMAC
        # verification). This plan delivers polling only; the probe just
        # logs whether push mode is available for this account.
        await _probe_openapi_visibility(comulytic)

        seen, bootstrapped = _load_seen(config.comulytic_state_file)
        _log.info(
            "bridge started (poll interval=%.1fs, page_size=%d, audio_path=%s, "
            "state_file=%s, seen=%d, bootstrapped=%s, discord_surface=%s)",
            config.comulytic_poll_interval_seconds,
            config.comulytic_poll_page_size,
            config.comulytic_audio_path,
            config.comulytic_state_file,
            len(seen),
            bootstrapped,
            "on" if rest is not None else "off (log-only)",
        )

        while True:
            try:
                bootstrapped = await poll_once(
                    comulytic,
                    opencode,
                    seen,
                    bootstrapped,
                    config.comulytic_state_file,
                    rest=rest,
                    router=router,
                )
            except ComulyticError as exc:
                _log.error("poll cycle failed: %s", exc)
            except (
                Exception
            ):  # noqa: BLE001 — a poll cycle crash must not kill the bridge
                _log.exception("poll cycle crashed (continuing)")
            await asyncio.sleep(config.comulytic_poll_interval_seconds)
    finally:
        _save_seen(config.comulytic_state_file, seen, bootstrapped)
        try:
            await router.save()
        except Exception:  # noqa: BLE001
            _log.warning("bridge SessionRouter save failed", exc_info=True)
        try:
            await aclose_slug_client()
        except Exception:  # noqa: BLE001
            _log.warning("slug client close failed", exc_info=True)
        try:
            if rest is not None:
                await rest.aclose()
        except Exception:  # noqa: BLE001
            _log.warning("discord rest close failed", exc_info=True)
        try:
            await comulytic.close()
        except Exception:  # noqa: BLE001
            _log.warning("comulytic client close failed", exc_info=True)
        try:
            await opencode.aclose()
        except Exception:  # noqa: BLE001
            _log.warning("opencode client close failed", exc_info=True)
        try:
            serve.stop()
        except Exception:  # noqa: BLE001
            _log.warning("opencode serve stop failed", exc_info=True)
        _log.info("bridge shut down cleanly")


# ---------------------------------------------------------------------------
# Openapi push-alternative probe (Step 4)
# ---------------------------------------------------------------------------


async def _probe_openapi_visibility(comulytic: ComulyticClient) -> None:
    """One cheap read-only probe of
    `GET /api/openapi/v1/api-keys/developer-tab/visibility`.

    2/3 HAR batches (FNOUHEA + AEIYDDL) confirm the `/api/openapi/v1/` surface
    exists in the client bundle and is account-gated via this endpoint. If
    `.data.visible == true`, log a NOTICE that push mode is available (and
    would need `POST /api/openapi/v1/api-keys/bootstrap` + webhook
    registration — a follow-up plan, adds `fastapi`+`uvicorn` deps). If
    `visible == false` or the call fails (404/403/exception): silently
    continue in poll mode. The exact auth header for the openapi surface
    (`Bearer` vs `X-API-Key`) is an unconfirmed gap — the probe uses the
    same Bearer header as the rest of the API (best-effort).
    """
    try:
        env = await comulytic._request(
            "GET", "/api/openapi/v1/api-keys/developer-tab/visibility"
        )
        data = env.get("data") if isinstance(env, dict) else None
        visible = bool(data.get("visible")) if isinstance(data, dict) else False
        if visible:
            _log.warning(
                "openapi push mode is AVAILABLE for this account (developer-tab "
                "visible=true). Implementing the push receiver is a follow-up "
                "plan (inbound FastAPI endpoint + HMAC verification + webhook "
                "registration). The polling bridge continues to run."
            )
        else:
            _log.info(
                "openapi push mode not available (visible=false) — poll mode only"
            )
    except ComulyticError as exc:
        _log.info("openapi visibility probe failed (%s) — poll mode only", exc)
    except Exception:  # noqa: BLE001 — probe is best-effort
        _log.info("openapi visibility probe errored — poll mode only", exc_info=True)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------


async def poll_once(
    comulytic: ComulyticClient,
    opencode: OpencodeClient,
    seen: set[str],
    bootstrapped: bool,
    state_path: str,
    *,
    rest: DiscordRest | None = None,
    router: SessionRouter | None = None,
) -> bool:
    """One poll cycle. Returns the (possibly updated) `bootstrapped` flag.

    1. Cheap probe: `total, newest = await comulytic.probe_newest()`.
    2. Bootstrap clause: if NOT bootstrapped, enumerate all pages, mark every
       noteId as seen, set bootstrapped=True, save, return. NO processing.
    3. If `total` unchanged AND `newest` in seen: log "no new recordings", return.
    4. Else enumerate all pages, collect new noteIds, update seen, filter to
       audio-delivered, process each (per-recording try/except), save seen.

    ``rest`` + ``router`` are passed through to ``process_new_recording`` so
    it can create a Discord channel + bind it when Discord is configured.
    Both may be None (log-only mode).
    """
    total, newest = await comulytic.probe_newest()

    # --- bootstrap clause ---
    if not bootstrapped:
        _log.info(
            "bootstrapping seen-set: marking all %d current recordings as seen", total
        )
        all_ids = await _enumerate_all_note_ids(comulytic, total)
        seen.update(all_ids)
        bootstrapped = True
        _save_seen(state_path, seen, bootstrapped)
        _log.info(
            "bootstrapped: marked %d existing recordings as seen; only recordings "
            "created after now will be routed",
            len(all_ids),
        )
        return bootstrapped

    # --- fast path: nothing new ---
    if newest is not None and newest in seen:
        _log.debug("no new recordings (total=%d, newest=%s in seen)", total, newest)
        return bootstrapped

    # --- slow path: enumerate + process new audio-delivered recordings ---
    page_size = config.comulytic_poll_page_size
    _log.info("total changed (total=%d) — enumerating pages", total)
    new_ids, observed = await _enumerate_new_note_ids(comulytic, total, page_size, seen)

    # Update seen with ALL observed noteIds (so a half-synced recording that
    # delivers next cycle is detected as "metadata change" not "new"). The
    # bootstrap clause guarantees seen only grows here.
    seen.update(observed)

    if not new_ids:
        _log.debug(
            "enumerated but no new noteIds (total=%d, observed=%d)",
            total,
            len(observed),
        )
        _save_seen(state_path, seen, bootstrapped)
        return bootstrapped

    _log.info("found %d new recording(s); filtering for audio-delivered", len(new_ids))

    # Filter to audio-delivered; non-delivered new ones are already in seen
    # (added above) and will be re-checked next cycle for delivery.
    # NOTE: `_is_note_audio_delivered` is async; awaiting it inline inside
    # the list-comprehension predicate would just build coroutine objects
    # (which are always truthy), so every recording would look "delivered"
    # and we'd never actually filter. Gather the awaits concurrently
    # instead — each note_id is unique so the per-note paging-cache `pop`s
    # hit distinct keys.
    delivery_flags = await asyncio.gather(
        *[_is_note_audio_delivered(comulytic, nid) for nid in new_ids]
    )
    delivered = [nid for nid, ok in zip(new_ids, delivery_flags) if ok]
    _log.info(
        "%d new recording(s) audio-delivered; %d not yet delivered (will re-check next cycle)",
        len(delivered),
        len(new_ids) - len(delivered),
    )

    for note_id in delivered:
        try:
            await process_new_recording(
                comulytic, opencode, note_id, rest=rest, router=router
            )
        except ComulyticError as exc:
            _log.error("recording %s failed: %s", note_id, exc)
        except (
            Exception
        ):  # noqa: BLE001 — one recording's crash must not kill the cycle
            _log.exception("recording %s crashed (continuing)", note_id)

    # Clear the paging-item cache so it can't leak across cycles (entries
    # already pop'd in `_is_note_audio_delivered` are gone; any leftover
    # non-delivered new noteIds are now in `seen` and won't be re-checked, so
    # their cached paging items are dead weight).
    _clear_paging_cache()
    _save_seen(state_path, seen, bootstrapped)
    return bootstrapped


async def _enumerate_all_note_ids(comulytic: ComulyticClient, total: int) -> list[str]:
    """Enumerate every page from 1..ceil(total/page_size) and return all noteIds.

    Used by the bootstrap clause. `total` can change mid-poll; re-fetch `total`
    each page defensively is overkill — the bootstrap is one-shot and a few
    stragglers are fine (they'll be picked up as "new" next cycle, which is the
    correct behavior for a recording created during the bootstrap).
    """
    page_size = config.comulytic_poll_page_size
    ids: list[str] = []
    pages = max(1, (total + page_size - 1) // page_size) if page_size > 0 else 1
    for page in range(1, pages + 1):
        try:
            env = await comulytic.list_recordings(page=page, page_size=page_size)
        except ComulyticError as exc:
            _log.warning("bootstrap page %d failed: %s", page, exc)
            continue
        data = (env.get("data") or {}) if isinstance(env, dict) else {}
        items = data.get("data") or []
        for item in items:
            if isinstance(item, dict) and item.get("noteId"):
                ids.append(str(item["noteId"]))
    return ids


async def _enumerate_new_note_ids(
    comulytic: ComulyticClient, total: int, page_size: int, seen: set[str]
) -> tuple[list[str], list[str]]:
    """Enumerate pages 1..ceil(total/page_size) and return `(new_ids, all_observed_ids)`.

    `new_ids` = observed noteIds NOT in `seen` (the candidates for processing).
    `all_observed_ids` = every noteId in the catalog (for updating `seen`).
    """
    new_ids: list[str] = []
    observed: list[str] = []
    pages = max(1, (total + page_size - 1) // page_size) if page_size > 0 else 1
    for page in range(1, pages + 1):
        try:
            env = await comulytic.list_recordings(page=page, page_size=page_size)
        except ComulyticError as exc:
            _log.warning("enumerate page %d failed: %s", page, exc)
            continue
        data = (env.get("data") or {}) if isinstance(env, dict) else {}
        items = data.get("data") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            nid = item.get("noteId")
            if not nid:
                continue
            nid_s = str(nid)
            observed.append(nid_s)
            if nid_s not in seen:
                # Stash the paging item on the id via a side-dict so the
                # audio-delivered check can reuse it without a re-fetch.
                _PAGING_ITEM_CACHE[nid_s] = item
                new_ids.append(nid_s)
    return new_ids, observed


# A small in-process cache of paging items for the current cycle, so
# `_is_note_audio_delivered` can reuse the already-fetched paging item instead
# of re-fetching noteDetail just for the audio-delivery predicate. Cleared at
# the end of each poll cycle.
_PAGING_ITEM_CACHE: dict[str, dict] = {}


def _clear_paging_cache() -> None:
    _PAGING_ITEM_CACHE.clear()


async def _is_note_audio_delivered(comulytic: ComulyticClient, note_id: str) -> bool:
    """Check the audio-delivery predicate for `note_id`.

    Prefer the cached paging item (from `_enumerate_new_note_ids`); fall back
    to a `get_note_detail` call only if the cache miss is real. The paging
    item carries `hasCloudAudio` + `audioAccess` per the HAR.
    """
    item = _PAGING_ITEM_CACHE.pop(note_id, None)
    if item is not None:
        return is_audio_delivered(item)
    # Cache miss — fetch noteDetail (heavier but correct).
    try:
        detail = await comulytic.get_note_detail(note_id)
    except ComulyticError as exc:
        _log.warning(
            "noteDetail for %s failed during audio-delivery check: %s", note_id, exc
        )
        return False
    return is_audio_delivered(detail)


# ---------------------------------------------------------------------------
# Per-recording pipeline
# ---------------------------------------------------------------------------


async def process_new_recording(
    comulytic: ComulyticClient,
    opencode: OpencodeClient,
    note_id: str,
    *,
    rest: DiscordRest | None = None,
    router: SessionRouter | None = None,
) -> dict:
    """The per-recording pipeline. Returns `{note_id, transcript, session_id, response}`.

    Local Whisper only — Comulytic's cloud ASR (`queryTranscribeResult`) is
    NOT consulted. The flow mirrors `/oc_talk` (`commands.py:_run_talk_session`):

    (a) `get_note_detail(note_id)` — needed for the Path A pre-signed audio
        URL and as the freshness check that audio is delivered. (The
        audio-delivery predicate `is_audio_delivered` is already checked in
        `poll_once` before this function is called, but `noteDetail` is still
        required to mint a Path A URL when `comulytic_audio_path == "presigned"`
        or when Path B fails and we fall back to Path A.)
    (b) `download_audio_smart` → `voice.extract_audio_to_wav` (ffmpeg →
        mono 16kHz WAV) → `voice.transcribe_audio` (dispatches on
        `voice_stt_provider`; default `"local"` = faster-whisper, in-process).
        This is the SAME pipeline `/oc_talk` runs on uploaded attachments.
    (c) Route the transcript to `oc-assistant` via `route_to_assistant`.

    ``rest`` + ``router`` are passed through to ``route_to_assistant`` so a
    Discord channel can be created + bound when Discord is configured. Both
    may be None (log-only mode).
    """
    _log.info("processing recording %s — downloading + local Whisper STT", note_id)
    try:
        detail = await comulytic.get_note_detail(note_id)
    except ComulyticError as exc:
        _log.error("recording %s: noteDetail failed: %s", note_id, exc)
        return {
            "note_id": note_id,
            "transcript": "",
            "session_id": None,
            "response": "",
        }
    return await _transcribe_and_route(
        comulytic, opencode, note_id, detail, rest=rest, router=router
    )


async def _transcribe_and_route(
    comulytic: ComulyticClient,
    opencode: OpencodeClient,
    note_id: str,
    detail: dict | None,
    *,
    rest: DiscordRest | None = None,
    router: SessionRouter | None = None,
) -> dict:
    """Download audio + local Whisper STT, then route the transcript to oc-assistant.

    The sole transcription path (mirrors `/oc_talk`). Imports
    `voice.transcribe_audio` + `voice.extract_audio_to_wav` (voice.py imports
    Pycord at module top — Pycord is a hard dep of this package). Uses
    `download_audio_smart` to pick Path B (primary, cookie-auth proxy) or
    Path A (fallback, pre-signed S3 URL with 48h TTL).
    """
    if detail is None:
        try:
            detail = await comulytic.get_note_detail(note_id)
        except ComulyticError as exc:
            _log.error("recording %s: noteDetail failed: %s", note_id, exc)
            return {
                "note_id": note_id,
                "transcript": "",
                "session_id": None,
                "response": "",
            }

    try:
        audio_bytes = await download_audio_smart(comulytic, note_id, detail)
    except ComulyticError as exc:
        _log.error("recording %s: audio download failed: %s", note_id, exc)
        return {
            "note_id": note_id,
            "transcript": "",
            "session_id": None,
            "response": "",
        }
    if not audio_bytes:
        _log.error("recording %s: audio download returned empty", note_id)
        return {
            "note_id": note_id,
            "transcript": "",
            "session_id": None,
            "response": "",
        }

    # --- Max-duration cap: mark seen but skip transcription of over-long
    # recordings. Guards against accidental recordings (e.g. a recorder left
    # on for hours) being sent through the expensive local Whisper STT pass.
    # The recording is already in `seen` (poll_once updates seen with ALL
    # observed noteIds before calling this function), so an early return here
    # leaves it marked as seen — it won't re-appear as "new" next cycle.
    # Fail-open: if the probe can't determine duration (ffprobe missing,
    # corrupt bytes, empty output), transcribe as the existing path does.
    cap = (
        config.comulytic_max_duration_hours * 3600
        + config.comulytic_max_duration_minutes * 60
        + config.comulytic_max_duration_seconds
    )
    if cap > 0:
        from opencode_discord_bot.voice import probe_audio_duration_seconds

        duration = await probe_audio_duration_seconds(
            audio_bytes, filename=f"{note_id}.mp3"
        )
        if duration is not None and duration > cap:
            _log.info(
                "recording %s: duration %.1fs exceeds cap %ds — marking seen, "
                "skipping transcription",
                note_id,
                duration,
                cap,
            )
            return {
                "note_id": note_id,
                "transcript": "",
                "session_id": None,
                "response": "",
                "skipped_reason": "duration_exceeds_cap",
            }

    from opencode_discord_bot.voice import extract_audio_to_wav
    from opencode_discord_bot.speakers import transcribe_with_speakers

    try:
        wav_bytes = await extract_audio_to_wav(
            audio_bytes, content_type="audio/mpeg", filename=f"{note_id}.mp3"
        )
        transcript = (await transcribe_with_speakers(wav_bytes) or "").strip()
    except Exception as exc:  # noqa: BLE001 — ffmpeg/whisper failures
        _log.error("recording %s: local Whisper STT failed: %s", note_id, exc)
        return {
            "note_id": note_id,
            "transcript": "",
            "session_id": None,
            "response": "",
        }

    if not transcript:
        _log.info("recording %s: local Whisper returned empty — skipping", note_id)
        return {
            "note_id": note_id,
            "transcript": "",
            "session_id": None,
            "response": "",
        }

    _log.info(
        "recording %s: transcript ready (%d chars) — routing to oc-assistant",
        note_id,
        len(transcript),
    )
    return await route_to_assistant(
        opencode, transcript, note_id, rest=rest, router=router
    )


async def download_audio_smart(
    comulytic: ComulyticClient, note_id: str, detail: dict
) -> bytes:
    """Audio-download picker.

    If `comulytic_audio_path == "proxy"` (default — Path B): try the cookie-auth
    proxy first, fall back to Path A on `ComulyticError` (retry once).

    Else (Path A) OR Path B failed: get the pre-signed S3 URL from `detail`,
    try `download_audio_presigned`; on `AudioUrlExpiredError` (S3 403) re-mint
    via `get_note_detail(note_id)` and retry once with a fresh URL.
    """
    if config.comulytic_audio_path == "proxy":
        try:
            audio_bytes = await comulytic.download_audio_proxy(note_id)
            if audio_bytes:
                return audio_bytes
            _log.warning(
                "recording %s: Path B returned empty — falling back to Path A", note_id
            )
        except ComulyticError as exc:
            _log.warning(
                "recording %s: Path B failed (%s) — falling back to Path A",
                note_id,
                exc,
            )
        # Fall through to Path A.

    # Path A: pre-signed S3 URL.
    for attempt in (0, 1):
        if attempt == 1:
            # Re-mint the URL via noteDetail (the prior URL returned 403).
            try:
                detail = await comulytic.get_note_detail(note_id)
            except ComulyticError as exc:
                raise ComulyticError(
                    f"Path A re-mint failed for {note_id}: {exc}"
                ) from exc
        enhanced, raw = comulytic.get_audio_url(detail)
        url = raw or enhanced  # prefer raw; avoid denoised enhancedAudio
        if not url:
            raise ComulyticError(f"recording {note_id}: no audio URL in noteDetail")
        try:
            return await comulytic.download_audio_presigned(url)
        except AudioUrlExpiredError as exc:
            if attempt == 0:
                _log.warning(
                    "recording %s: Path A URL expired (403) — re-minting", note_id
                )
                continue
            raise ComulyticError(
                f"Path A re-mint retry failed for {note_id}: {exc}"
            ) from exc
    raise ComulyticError(f"Path A exhausted retries for {note_id}")


# ---------------------------------------------------------------------------
# oc-assistant routing
# ---------------------------------------------------------------------------


def _status_to_progress_text(status: dict) -> str:
    """Render a short progress line from a session status dict.

    Copy of ``commands.py:_status_to_progress_text`` (pure, no Discord imports)
    so the bridge's progress-edit closure stays self-contained. Returns "" for
    idle/unknown.
    """
    stype = (status or {}).get("type") or ""
    if stype == "busy":
        return "busy…"
    if stype == "retry":
        attempt = status.get("attempt")
        message = status.get("message")
        if attempt is not None:
            base = f"retrying (attempt {attempt})"
        else:
            base = "retrying"
        return f"{base}: {message}" if message else base
    return ""


async def _rename_when_slug_ready(
    rest: DiscordRest,
    channel_id: int,
    prompt: str,
    fallback: str,
) -> None:
    """Generate an LLM slug from `prompt` and rename the Discord channel to it.

    Mirrors ``commands.py:OpencodeBot._rename_when_slug_ready`` (the
    /oc_talk fire-and-forget rename) but uses raw Discord REST instead of
    ``TextChannel.edit``. Calls ``generate_slug`` (one short chat completion
    on the small cloud model), and if the result differs from the channel's
    current name and is non-empty, edits the channel via
    ``rest.edit_channel``. Best-effort: a failed rename logs a WARNING and
    leaves the initial name in place — channel creation is never blocked by
    this call (callers fire it via ``asyncio.create_task``, not ``await``).
    """
    try:
        slug = await generate_slug(prompt, fallback=fallback)
    except Exception as exc:  # noqa: BLE001 — generate_slug is supposed to never raise
        _log.warning("generate_slug raised unexpectedly; keeping fallback: %s", exc)
        return
    if not slug or slug == fallback:
        return
    try:
        await rest.edit_channel(channel_id, slug, reason="LLM slug upgrade")
    except DiscordRestError as exc:
        _log.warning(
            "channel rename to %r failed (keeping %r): %s", slug, fallback, exc
        )


async def route_to_assistant(
    opencode: OpencodeClient,
    transcript: str,
    note_id: str,
    *,
    rest: DiscordRest | None = None,
    router: SessionRouter | None = None,
) -> dict:
    """Route `transcript` to opencode's `oc-assistant` agent.

    Mirrors ``commands.py:_run_talk_session`` (the `/oc_talk` routing
    sequence), using raw Discord REST when ``rest`` is provided (no Pycord,
    no gateway — safe to run alongside the main bot). When ``rest`` is None
    (Discord not configured), falls back to the log-only path.

    With Discord:
      1. Create an opencode session titled `comulytic-<short-noteId>`.
      2. Create a Discord text channel (regex slug from the transcript,
         category from `discord_bot_session_category_id`, topic mentions
         the session id + note id).
      3. Bind channel -> session via the bridge-owned ``router`` (separate
         persistence file from the main bot).
      4. Post the transcript in the channel.
      5. Fire-and-forget LLM slug rename (``_rename_when_slug_ready``).
      6. Post a "Working on session…" progress message.
      7. Build the prompt (optionally prepend `[PLAN_TYPE_PRESELECTED]`) and
         `send_prompt_async(sid, parts, agent="oc-assistant")`.
      8. Concurrently: `poll_until_idle` (throttled progress-edit on_status)
         + `poll_pending_requests_rest` (surfaces oc-assistant clarifying
         questions as plain-text prompts in the channel, polls for the
         user's reply, calls reply_question/reject_question).
      9. `list_messages` + `_final_assistant_text`, post the response in
         chunks via `_split_message`.
     10. Optional pointer to `comulytic_discord_pointer_channel_id`.

    Without Discord (rest is None): create session, send prompt, poll until
    idle, fetch final text, LOG the response (the original behavior —
    graceful degradation when Discord isn't configured).
    """
    short_id = note_id[:8] if note_id else "unknown"
    try:
        session = await opencode.create_session(title=f"comulytic-{short_id}")
    except OpencodeError as exc:
        _log.error("recording %s: create_session failed: %s", note_id, exc)
        return {
            "note_id": note_id,
            "transcript": transcript,
            "session_id": None,
            "response": "",
        }
    sid = session.get("id") if isinstance(session, dict) else None
    if not sid:
        _log.error("recording %s: create_session returned no id: %r", note_id, session)
        return {
            "note_id": note_id,
            "transcript": transcript,
            "session_id": None,
            "response": "",
        }

    # --- Discord surface gate ---
    discord_active = (
        rest is not None
        and router is not None
        and bool(config.discord_bot_token)
        and config.discord_bot_guild_id != 0
    )
    channel_id: int | None = None
    if not discord_active:
        _log.info(
            "recording %s: Discord surface not configured (rest=%s, router=%s, "
            "token=%s, guild_id=%s) — routing to oc-assistant and LOGGING the "
            "response only",
            note_id,
            "on" if rest is not None else "off",
            "on" if router is not None else "off",
            "set" if config.discord_bot_token else "empty",
            config.discord_bot_guild_id,
        )

    # Build the prompt with the optional plan-type directive (sent regardless
    # of whether Discord is active).
    # The [COMULYTIC_BRIDGE] directive is ALWAYS prepended so oc-assistant
    # knows this session is on the plain-text-reply path (buttons cannot be
    # used — the bridge doesn't own the gateway connection and so can't
    # receive component-interaction events). oc-assistant constrains its
    # clarifying-question behavior accordingly (one question per call, not
    # the 1-3 batch allowed on the button path). See .opencode/agent/oc-assistant.md
    # "Comulytic bridge (plain-text replies, no buttons)".
    prompt = transcript
    plan_type = (
        config.comulytic_plan_type.strip().lower() if config.comulytic_plan_type else ""
    )
    if plan_type in ("actionable", "note"):
        directive = (
            "[PLAN_TYPE_PRESELECTED: actionable]"
            if plan_type == "actionable"
            else "[PLAN_TYPE_PRESELECTED: note]"
        )
        prompt = f"[DISCORD_BOT]\n[COMULYTIC_BRIDGE]\n{directive}\n\n{transcript}"
    else:
        prompt = f"[DISCORD_BOT]\n[COMULYTIC_BRIDGE]\n\n{transcript}"

    # --- Create the Discord channel + post the transcript BEFORE the prompt ---
    progress_msg_id: int | None = None
    if discord_active and rest is not None:
        assert router is not None  # narrowed by discord_active
        slug = _slugify_prompt(transcript, fallback=f"comulytic-{short_id}")
        try:
            ch = await rest.create_text_channel(
                config.discord_bot_guild_id,
                slug,
                parent_id=config.discord_bot_session_category_id,
                topic=f"opencode comulytic session {sid} (note {note_id})",
                reason="comulytic bridge auto-route",
            )
        except DiscordRestError as exc:
            _log.error(
                "recording %s: create_text_channel failed: %s — falling back to log-only",
                note_id,
                exc,
            )
            discord_active = False
        else:
            raw_cid = ch.get("id") if isinstance(ch, dict) else None
            if raw_cid is None:
                _log.error(
                    "recording %s: create_text_channel returned no id: %r — log-only",
                    note_id,
                    ch,
                )
                discord_active = False
            else:
                try:
                    channel_id = int(raw_cid)
                except (TypeError, ValueError):
                    _log.error(
                        "recording %s: create_text_channel id not an int: %r — log-only",
                        note_id,
                        raw_cid,
                    )
                    discord_active = False

        if discord_active and channel_id is not None:
            try:
                await router.bind(channel_id, sid)
            except Exception as exc:  # noqa: BLE001 — bind is best-effort
                _log.warning("recording %s: router.bind failed: %s", note_id, exc)

            # Post the transcript (chunked via _split_message).
            transcript_header = f"**Transcribed prompt (note `{note_id}`):**\n```\n"
            transcript_footer = "\n```"
            for chunk in _split_message(
                transcript_header + transcript + transcript_footer
            ):
                try:
                    await rest.create_message(channel_id, chunk)
                except DiscordRestError as exc:
                    _log.warning(
                        "recording %s: post transcript chunk failed: %s",
                        note_id,
                        exc,
                    )
                    break

            # Fire-and-forget LLM slug rename (mirrors /oc_talk).
            asyncio.create_task(
                _rename_when_slug_ready(rest, channel_id, transcript, fallback=slug)
            )

            # Post the "Working on session…" progress message (edited by
            # on_status during poll_until_idle).
            try:
                progress_msg = await rest.create_message(
                    channel_id, f"Working on session `{sid}`…"
                )
                raw_pmid = (
                    progress_msg.get("id") if isinstance(progress_msg, dict) else None
                )
                if raw_pmid is not None:
                    try:
                        progress_msg_id = int(raw_pmid)
                    except (TypeError, ValueError):
                        progress_msg_id = None
            except DiscordRestError as exc:
                _log.warning(
                    "recording %s: post progress message failed: %s",
                    note_id,
                    exc,
                )

    # --- Send the prompt to oc-assistant ---
    parts = [{"type": "text", "text": prompt}]
    try:
        await opencode.send_prompt_async(sid, parts, agent="oc-assistant")
    except OpencodeError as exc:
        _log.error("recording %s: send_prompt_async failed: %s", note_id, exc)
        if discord_active and channel_id is not None and rest is not None:
            try:
                await rest.create_message(
                    channel_id, f"Failed to send prompt to oc-assistant: {exc}"
                )
            except DiscordRestError:
                pass
        return {
            "note_id": note_id,
            "transcript": transcript,
            "session_id": sid,
            "response": "",
        }

    # Mark this sid as bridge-driven so the main bot's `on_message` yields
    # silently to our REST reply-poller instead of re-dispatching each
    # clarifying-question answer as a fresh `send_prompt_async` follow-up
    # (the dual-consumer race that produced "Session is busy" errors and
    # duplicate final-response messages). Cleared in the `finally` wrapping
    # the drive below. See opencode_discord_bot.bridge_state.
    mark_active(sid)

    _log.info(
        "recording %s: prompt sent to oc-assistant (session=%s, discord=%s) — "
        "waiting for completion",
        note_id,
        sid,
        "on" if discord_active else "off",
    )

    # --- Concurrent: poll pending questions/permissions (Discord surface) ---
    stop_event = asyncio.Event()
    poller: asyncio.Task | None = None
    _last_progress_edit = 0.0

    async def _on_status(status: dict) -> None:
        text = _status_to_progress_text(status)
        if (
            not text
            or progress_msg_id is None
            or rest is None
            or channel_id is None
            or not discord_active
        ):
            _log.debug("session %s status: %s", sid, status)
            return
        # Throttle progress edits (mirrors commands.py:PROGRESS_EDIT_MIN_INTERVAL).
        nonlocal _last_progress_edit
        now = asyncio.get_event_loop().time()
        if now - _last_progress_edit < _PROGRESS_EDIT_MIN_INTERVAL:
            return
        _last_progress_edit = now
        try:
            await rest.edit_message(
                channel_id,
                progress_msg_id,
                f"Working on session `{sid}`…\n{text}",
            )
        except DiscordRestError as exc:
            _log.debug("progress edit failed: %s", exc)

    if discord_active and channel_id is not None and rest is not None:
        # Resolve the bot's own user id so the reply-poller can filter the
        # bot's own posted messages out. GET /users/@me via Discord REST.
        bot_user_id = await _resolve_bot_user_id(rest)

        poller = asyncio.create_task(
            poll_pending_requests_rest(
                opencode,
                sid,
                rest,
                channel_id,
                stop_event=stop_event,
                interval=config.comulytic_question_poll_interval_seconds,
                question_timeout=config.comulytic_question_timeout_seconds,
                bot_user_id=bot_user_id,
            )
        )

    # --- Drive the session to idle, then post the final response ---
    # Wrapped in try/finally so `clear_active(sid)` always runs — on success,
    # timeout, OpencodeError, and cancellation (the bot's `close()` cancels
    # the in-process bridge task). Once the drive ends the main bot's
    # `on_message` may resume driving follow-ups for this channel.
    try:
        # --- Poll until idle ---
        try:
            await poll_until_idle(opencode, sid, _on_status, timeout=_ASSISTANT_TIMEOUT)
        except asyncio.TimeoutError:
            _log.warning(
                "recording %s: oc-assistant timed out after %.0fs — fetching partial output",
                note_id,
                _ASSISTANT_TIMEOUT,
            )
        except OpencodeError as exc:
            _log.error("recording %s: poll_until_idle failed: %s", note_id, exc)

        # Stop the question poller.
        stop_event.set()
        if poller is not None:
            try:
                await asyncio.wait_for(poller, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                poller.cancel()

        # --- Fetch the final assistant text (with retry for the timing race) ---
        # poll_until_idle returns "idle" once the session's status map entry
        # clears, but the assistant message's text part may not be persisted
        # in list_messages yet (the fire-and-forget prompt_async gap that
        # events.py guards against but can't fully close). Re-fetch a few
        # times before concluding the agent emitted no text. Each attempt
        # catches OpencodeError so a transient list_messages failure doesn't
        # abort the retry loop (mirrors poll_until_idle's error tolerance).
        final = ""
        for attempt in range(_FINAL_TEXT_RETRY_ATTEMPTS):
            try:
                messages = await opencode.list_messages(sid)
            except OpencodeError as exc:
                _log.warning(
                    "recording %s: list_messages attempt %d/%d failed: %s",
                    note_id,
                    attempt + 1,
                    _FINAL_TEXT_RETRY_ATTEMPTS,
                    exc,
                )
                messages = None
            final = _final_assistant_text(messages) if messages else ""
            if final and not _looks_like_prompt(final):
                break
            if attempt < _FINAL_TEXT_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_FINAL_TEXT_RETRY_SLEEP)

        # Detect a prompt leak (regression guard): if the extracted text
        # still carries the [DISCORD_BOT]/[COMULYTIC_BRIDGE] directive tags
        # it's the user prompt, not an agent reply. _final_assistant_text no
        # longer falls back to non-assistant messages, so this should never
        # fire — but if a future change re-introduces a fallback, catch it
        # here instead of posting the transcript as the "response".
        if final and _looks_like_prompt(final):
            _log.error(
                "recording %s: extracted 'final' looks like the user prompt "
                "(contains directive tags) — suppressing the leak. Session %s "
                "is idle; the agent may not have emitted a summary text part. "
                "Check .opencode/assistant/ for the artifact.",
                note_id,
                sid,
            )
            final = ""

        _log.info(
            "recording %s: oc-assistant done (session=%s, response_len=%d)",
            note_id,
            sid,
            len(final),
        )
        if final:
            _log.info("oc-assistant response for %s:\n%s", note_id, final)

        # --- Post the response to the Discord channel ---
        if discord_active and channel_id is not None and rest is not None:
            if not final:
                try:
                    await rest.create_message(
                        channel_id,
                        f"No agent text output found — the agent may not have "
                        f"emitted a summary. Session `{sid}` is idle; check "
                        f"`.opencode/assistant/` for the artifact.",
                    )
                except DiscordRestError as exc:
                    _log.warning("recording %s: post done-msg failed: %s", note_id, exc)
            else:
                prefix = f"**opencode** (session `{sid}`):\n"
                for chunk in _split_message(prefix + final):
                    try:
                        await rest.create_message(channel_id, chunk)
                    except DiscordRestError as exc:
                        _log.warning(
                            "recording %s: post response chunk failed: %s", note_id, exc
                        )
                        break

            # Optional pointer to the configured trigger channel.
            pointer_id = config.comulytic_discord_pointer_channel_id
            if pointer_id:
                try:
                    await rest.create_message(
                        pointer_id,
                        f"Created <#{channel_id}> — routed Comulytic note `{note_id}` to oc-assistant.",
                    )
                except DiscordRestError as exc:
                    _log.warning("recording %s: post pointer failed: %s", note_id, exc)

        return {
            "note_id": note_id,
            "transcript": transcript,
            "session_id": sid,
            "response": final,
        }
    finally:
        clear_active(sid)


async def _resolve_bot_user_id(rest: DiscordRest) -> int | None:
    """Resolve the bot's own Discord user id via GET /users/@me.

    Used by the question-reply poller to filter the bot's own posted messages
    out of the channel message list (so we don't pick up our own question/
    progress messages as the "reply"). Returns None on any failure (the
    poller then falls back to filtering ``author.bot == True``).
    """
    try:
        # /users/@me is not in the DiscordRest surface (it's a one-off);
        # reach into the underlying client directly.
        resp = await rest._client.get(f"{rest._base}/users/@me")
        if 200 <= resp.status_code < 300:
            data = resp.json()
            raw = data.get("id") if isinstance(data, dict) else None
            if raw is not None:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Small datetime helper
# ---------------------------------------------------------------------------


def _now_utc():
    import datetime as _dt

    return _dt.datetime.now(tz=_dt.timezone.utc)


# ---------------------------------------------------------------------------
# __main__ guard
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    main()
