"""Comulytic cloud API client — read-only poller for the Note Pro recordings catalog.

A plain `httpx.AsyncClient` wrapper (no Discord/Pycord dependency) targeting
`api.comulytic.ai` (the cloud API host) and `web.comulytic.ai` (the audio proxy
host). Reverse-engineered from a 3-batch consolidated HAR report
(VJNJGJV + FNOUHEA + AEIYDDL, 2026-08-02); see `.opencode/plans/comulytic-bridge.md`
in the main multiAgent repo for the full provenance + per-surface attribution.

Key surfaces:
- Auth: Bearer JWT (HS256, 150-day access TTL, 365-day refresh JWT) in the
  `authorization` header. The access JWT is captured from a real login at
  web.comulytic.ai (see SETUP_GUIDE.md "Comulytic bridge"). The refresh *call*
  is a capture gap — until re-captured, the bridge re-runs the full login
  before `exp`.
- Recordings catalog: `POST /api/kirby/v2/note/paging` — offset pagination,
  page 1 = newest, 1-indexed. Envelope `{code, data:{data, total, page,
  pageSize}, success}`.
- Note detail: `GET /api/kirby/v2/note/noteDetail?noteId=<id>` — the source of
  the pre-signed S3 audio URL (Path A); also confirms the recording exists
  and audio is delivered.
- Audio download (Path B PRIMARY): `GET {web_base}/api/note/audio-range/{noteId}`
  with the Bearer JWT in a cookie (`authorization=Bearer%20<jwt>`, URL-encoded).
  Stable `noteId`-based URL, no embedded expiry; only the JWT cookie (~150 days)
  rotates. Single 206 returns the complete MP3.
- Audio download (Path A FALLBACK): pre-signed S3 URL via `noteDetail`, 48h TTL
  (`X-Amz-Expires=172800`). Re-mint each cycle via `noteDetail`.

The bridge transcribes LOCALLY via `voice.transcribe_audio` (faster-whisper
by default) — Comulytic's cloud ASR (`queryTranscribeResult` /
`data.asrResultVO`) is NOT consumed by this client. Transcription happens
entirely on the host running the bridge, mirroring `/oc_talk`.

`acw_tc` WAF cookie is auto-minted on every qualifying response (30-min Max-Age,
HttpOnly) — the `httpx.AsyncClient` cookie jar handles capture-and-replay
automatically; no manual cookie handling.

Read-only invariant: this module NEVER calls `POST /note/updateNote` (a write
endpoint). Only `GET noteDetail`, `POST note/paging` (read-only query), and
the audio-range GET are used.

This module imports only stdlib + `httpx` so importing it has no heavy deps
(no Pycord, no faster-whisper) — mirrors the bot's lightweight-import
convention. Text helpers (`_extract_text`, `_final_assistant_text`) live in
`text_utils.py` (a Pycord-free shared module) — the bridge imports them from
there, not from `commands.py` (which imports Pycord at module top).
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import urllib.parse

import httpx

_log = logging.getLogger("comulytic")


class ComulyticError(Exception):
    """Raised on non-2xx / non-JSON responses from the Comulytic cloud API."""


class AudioUrlExpiredError(ComulyticError):
    """Raised when the pre-signed S3 audio URL (Path A) returns 403 (expired).

    The caller should re-mint a fresh URL via `get_note_detail(note_id)` and
    retry once. Path B (cookie-auth proxy) does not raise this — its URL is
    stable with no embedded expiry.
    """


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def jwt_expiry(jwt: str) -> _dt.datetime:
    """Decode the HS256 JWT payload (without signature verification) and return
    the `exp` claim as a UTC datetime.

    The access JWT has a 150-day TTL (`exp - iat = 12960000s`); the refresh JWT
    has a 365-day TTL (AEIYDDL confirmed both). The bridge surfaces impending
    access-JWT expiry to the user so they can re-login before all calls 401.

    Raises `ComulyticError` on a malformed JWT (wrong segment count or
    non-base64 payload). Signature verification is intentionally skipped — the
    bridge is a client of Comulytic's API, not a verifier of its tokens.
    """
    parts = jwt.split(".")
    if len(parts) < 2:
        raise ComulyticError("malformed JWT: expected 3 segments, got fewer")
    payload_b64 = parts[1]
    # JWT uses base64url without padding; add padding to length % 4.
    pad = (-len(payload_b64)) % 4
    payload_b64_padded = payload_b64 + ("=" * pad)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ComulyticError(f"malformed JWT payload: {exc}") from exc
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise ComulyticError("malformed JWT: missing or non-numeric 'exp' claim")
    return _dt.datetime.fromtimestamp(int(exp), tz=_dt.timezone.utc)


# ---------------------------------------------------------------------------
# Delivery predicate
# ---------------------------------------------------------------------------


def is_audio_delivered(item: dict) -> bool:
    """Audio-delivery predicate (gate audio download).

    True iff `hasCloudAudio == true AND audioAccess == "public"`. FNOUHEA
    proved with note `1bb61861` (UNTRANSCRIBED — `detailGenerationComplete:false,
    transStatus:false, transcribeStatus:0`) that audio download returned a
    complete 79,820-byte MP3 — transcription/summary flags are AI-pipeline
    signals, NOT audio-delivery signals, and must not gate audio download.

    `hasCloudAudio:false` → no cloud object yet; `audioAccess != "public"` →
    object present but not released. Do NOT gate audio download on
    `detailGenerationComplete` or transcription flags.
    """
    return item.get("hasCloudAudio") is True and item.get("audioAccess") == "public"


# ---------------------------------------------------------------------------
# Pre-signed S3 URL expiry parsing (Path A)
# ---------------------------------------------------------------------------


def parse_audio_url_expiry(url: str) -> _dt.datetime | None:
    """Parse the S3 pre-signed URL's `X-Amz-Date` + `X-Amz-Expires` query params
    and return the expiry as a UTC datetime, or None if unparseable.

    `X-Amz-Date` is `YYYYMMDDTHHMMSSZ`; `X-Amz-Expires` is seconds (e.g.
    `172800` = 48h). Used by the bridge to proactively re-mint Path A URLs
    before the 48h TTL elapses. Path B URLs have no such params (stable).
    """
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        amz_date_str = (qs.get("X-Amz-Date") or [None])[0]
        expires_str = (qs.get("X-Amz-Expires") or [None])[0]
        if not amz_date_str or not expires_str:
            return None
        amz_date = _dt.datetime.strptime(amz_date_str, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
        expires = int(expires_str)
        return amz_date + _dt.timedelta(seconds=expires)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# ComulyticClient
# ---------------------------------------------------------------------------


# Default desktop Chrome UA — used when `comulytic_user_agent` is empty. Must
# match the UA used during JWT capture to minimize fingerprint mismatch.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Audio download cap (Path A + Path B). The largest observed recording in the
# HAR was ~86 MB; 120 MB is a defensive ceiling.
_MAX_AUDIO_BYTES = 120 * 1024 * 1024


class ComulyticClient:
    """Async read-only client over the Comulytic cloud API.

    One `httpx.AsyncClient` is owned per instance and reused for the process
    lifetime — its built-in cookie jar auto-captures + resends `acw_tc` (the
    Aliyun WAF passive-challenge cookie) AND mirrors the
    `authorization=Bearer%20<jwt>` cookie the Path B audio proxy needs.

    Constructor args:
        base_url:    `https://api.comulytic.ai` (Bearer JWT host).
        jwt:         the 150-day access JWT.
        user_agent:  value for both `User-Agent` and `x-device-model`. Empty
                     → use `_DEFAULT_UA`.
        web_base:    `https://web.comulytic.ai` (Path B audio proxy host).
        refresh_token: the 365-day refresh JWT (empty if not captured; the
                     bridge treats the access JWT as non-refreshable until the
                     refresh *call* is captured).
        timeout:     per-request timeout (seconds).
    """

    def __init__(
        self,
        base_url: str,
        jwt: str,
        user_agent: str,
        *,
        web_base: str = "https://web.comulytic.ai",
        refresh_token: str = "",
        timeout: float = 30.0,
    ) -> None:
        if not jwt:
            raise ComulyticError("jwt is empty — cannot construct ComulyticClient")
        self._base_url = base_url.rstrip("/")
        self._web_base = web_base.rstrip("/")
        self._refresh_token = refresh_token
        ua = user_agent or _DEFAULT_UA
        # Mandatory header set (confirmed across all three HAR batches).
        # Origin/Referer MUST be pinned exactly or the allow-credentials CORS
        # check fails. Do NOT add X-App-Version/X-App-Channel/X-Device-Id/
        # X-OS-Version/X-TimeZone/X-User-Id/X-Gateway-Secret — the web client
        # never sends them (the userInfo cookie carries them all empty).
        self._headers = {
            "app-language": "en-us",
            "x-platform": "web",
            "x-device-model": ua,
            "authorization": f"Bearer {jwt}",
            "Origin": "https://web.comulytic.ai",
            "Referer": "https://web.comulytic.ai/",
            "User-Agent": ua,
            "cache-control": "no-cache",
            "pragma": "no-cache",
        }
        # The Path B audio proxy authenticates via a cookie, not a header:
        # `authorization=Bearer%20<jwt>` (URL-encoded). Set it on the cookie
        # jar so every request to web.comulytic.ai carries it. The same JWT
        # is also in the `authorization` header for api.comulytic.ai calls.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=30.0),
            # Follow redirects (audio-range may redirect to S3 on some paths).
            follow_redirects=True,
        )
        # Set the auth cookie on the jar. httpx's cookie jar is per-client and
        # persists across requests — exactly what the proxy needs.
        self._client.cookies.set(
            "authorization",
            f"Bearer%20{jwt}",  # URL-encoded "Bearer <jwt>"
            domain=urllib.parse.urlparse(self._web_base).hostname or "web.comulytic.ai",
        )

    async def close(self) -> None:
        """Close the underlying `httpx.AsyncClient`."""
        await self._client.aclose()

    # --- low-level request ---

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        extra_headers: dict | None = None,
    ) -> dict:
        """Inject mandatory headers + Bearer, send the request, parse JSON.

        Cookies (`acw_tc` + the `authorization` cookie) are handled by the
        `httpx.AsyncClient` jar automatically. On non-200 OR a non-JSON body
        (WAF interstitial/405), raise `ComulyticError`. On `success: false` in
        the parsed body, raise `ComulyticError` with the `msg` field.

        Honors `x-ratelimit-remaining`/`x-ratelimit-reset` + `Retry-After`
        defensively: if `remaining` approaches 0 OR status==429 OR
        `Retry-After` present, sleep and retry once; raise if it persists.
        No `OPTIONS` preflight (server-side, non-browser).
        """
        headers = dict(self._headers)
        if extra_headers:
            headers.update(extra_headers)
        # Don't send the auth cookie as a cookie on api.comulytic.ai calls —
        # the header carries it there. The cookie is only for the web proxy.
        # (httpx sends cookies matching the domain; the api host != web host,
        # so the cookie won't be sent on api calls anyway — this is defensive.)

        for attempt in (0, 1):
            try:
                resp = await self._client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt == 0:
                    _log.warning(
                        "transport error on %s %s: %r — retrying", method, path, exc
                    )
                    await _retry_sleep(resp=None)
                    continue
                raise ComulyticError(f"{method} {path} transport error: {exc}") from exc

            # Defensive rate-limit handling: 429 OR a near-exhausted remaining
            # quota OR an explicit Retry-After → sleep once and retry.
            retry_after = resp.headers.get("Retry-After")
            remaining = resp.headers.get("x-ratelimit-remaining")
            reset_ = resp.headers.get("x-ratelimit-reset")
            if attempt == 0 and (
                resp.status_code == 429
                or retry_after is not None
                or (
                    remaining is not None
                    and remaining.isdigit()
                    and int(remaining) < 10
                )
            ):
                wait = _retry_sleep(resp=resp)
                _log.warning(
                    "%s %s rate-limited (429/remaining=%s/retry-after=%s) — sleeping %.1fs",
                    method,
                    path,
                    remaining,
                    retry_after,
                    wait,
                )
                await _async_sleep(wait)
                continue

            if resp.status_code >= 400:
                raise ComulyticError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
                )
            # Detect non-JSON (WAF interstitial / 405 HTML page) before parsing.
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype and not resp.content:
                return {}
            if "json" not in ctype:
                raise ComulyticError(
                    f"{method} {path} -> non-JSON response "
                    f"(content-type={ctype!r}, len={len(resp.content)}): "
                    f"{resp.text[:300]}"
                )
            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ComulyticError(
                    f"{method} {path} -> JSON parse error: {exc}; body: {resp.text[:300]}"
                ) from exc
            if isinstance(data, dict) and data.get("success") is False:
                msg = data.get("msg") or data.get("message") or "(no msg)"
                raise ComulyticError(f"{method} {path} -> success:false: {msg}")
            return data  # type: ignore[return-value]
        # Unreachable — the loop runs at most twice.
        raise ComulyticError(f"{method} {path} -> exhausted retries")

    # --- recordings catalog ---

    async def list_recordings(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        trash: bool = False,
        dir_id: str = "",
        trans_success: bool | None = None,
    ) -> dict:
        """`POST /api/kirby/v2/note/paging` — offset pagination, page 1 = newest.

        Body `{page, pageSize, trash, dirId}` (+ `transSuccess: false` when
        `trans_success is False` — the cheap "still-pending" worklist filter;
        caveat: a note flips *out* of that bucket once `transStatus` becomes
        true even if `detailGenerationComplete` is not yet true, so re-check
        readiness). Returns the full envelope
        `{code, data:{data, total, page, pageSize}, success}`.
        """
        body: dict = {
            "page": page,
            "pageSize": page_size,
            "trash": trash,
            "dirId": dir_id,
        }
        if trans_success is False:
            body["transSuccess"] = False
        return await self._request("POST", "/api/kirby/v2/note/paging", json_body=body)

    async def probe_newest(self) -> tuple[int, str | None]:
        """Cheap change-detect: `list_recordings(page=1, page_size=1)` →
        `(total, data.data[0].noteId if data.data else None)`.

        One POST returning `total` + the newest `noteId`; the runner compares
        `total` to the prior cycle to decide whether to enumerate.
        """
        env = await self.list_recordings(page=1, page_size=1)
        data = (env.get("data") or {}) if isinstance(env, dict) else {}
        total = data.get("total") or 0
        items = data.get("data") or []
        newest = None
        if items and isinstance(items, list):
            first = items[0]
            if isinstance(first, dict):
                newest = first.get("noteId")
        return int(total), (str(newest) if newest is not None else None)

    # --- note detail ---

    async def get_note_detail(self, note_id: str) -> dict:
        """`GET /api/kirby/v2/note/noteDetail?noteId=<id>` → `data` block.

        Contains `audioUrl`, `objStorageUrl`, `audioUrlVO`, `transcribeStatus`,
        `transStatus`, `transFailureReason`, `transFailureMessage`,
        `detailGenerationComplete`, `summaryStatus`, `notSufficientToGenerateContent`.
        The source of the pre-signed S3 audio URL (Path A); also confirms the
        recording exists and audio is delivered.
        """
        env = await self._request(
            "GET", "/api/kirby/v2/note/noteDetail", params={"noteId": note_id}
        )
        if isinstance(env, dict) and isinstance(env.get("data"), dict):
            return env["data"]
        return {}

    # --- audio URL helpers (Path A) ---

    @staticmethod
    def get_audio_url(detail: dict) -> tuple[str | None, str | None]:
        """Return `(enhanced_url, raw_url)` from a `noteDetail` `data` block.

        `detail.audioUrl == detail.audioUrlVO.enhanceAudio` (the denoised MP3)
        per the HAR correlation; both are pre-signed S3 URLs with 48h TTL.
        Select `rawAudio`/`audioUrl` for transcription; avoid `enhanceAudio`
        (denoised) unless an enhanced transcript is explicitly desired.
        """
        audio_vo = detail.get("audioUrlVO") or {}
        enhanced = detail.get("audioUrl") or audio_vo.get("enhanceAudio")
        raw = detail.get("objStorageUrl") or audio_vo.get("rawAudio")
        return (enhanced or None, raw or None)

    # --- audio download (Path B PRIMARY, Path A FALLBACK) ---

    async def download_audio_proxy(
        self, note_id: str, *, max_bytes: int = _MAX_AUDIO_BYTES
    ) -> bytes:
        """Path B (PRIMARY): `GET {web_base}/api/note/audio-range/{note_id}`.

        Stable `noteId`-based URL with no embedded expiry; only the Bearer JWT
        cookie (~150 days) rotates. Headers `Range: bytes=0-`,
        `Accept-Encoding: identity`, `Cache-Control: no-cache`; the
        `authorization=Bearer%20<jwt>` cookie is on the jar (set in the
        constructor). Server returns a single 206 with
        `Content-Range: bytes 0-{N-1}/{N}` (the COMPLETE file — observed full
        sizes 59 MB / 79 KB / 85 MB / 86 MB). Stream the body, enforcing a
        `max_bytes` cap (default 120 MB).
        """
        url = f"{self._web_base}/api/note/audio-range/{note_id}"
        headers = {
            "Range": "bytes=0-",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            # The cookie jar supplies `authorization`; keep the header too for
            # robustness (some proxies read the header, others the cookie).
        }
        chunks: list[bytes] = []
        total = 0
        async with self._client.stream("GET", url, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ComulyticError(f"GET {url} -> {resp.status_code}: {body[:300]!r}")
            # 206 (Partial Content) is the expected success; 200 is also fine.
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ComulyticError(
                        f"audio-range {note_id} exceeded {max_bytes} byte cap "
                        f"(got {total})"
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    async def download_audio_presigned(
        self, url: str, *, max_bytes: int = _MAX_AUDIO_BYTES
    ) -> bytes:
        """Path A (FALLBACK): `GET <presigned S3 url>` with `Range: bytes=0-`.

        No `Authorization` header — auth is embedded in the signed query
        string. Server returns HTTP 206 Partial Content; stream the body,
        enforcing a `max_bytes` cap. On HTTP 403, raise
        `AudioUrlExpiredError` so the caller re-mints via `get_note_detail`.
        """
        headers = {"Range": "bytes=0-", "Accept-Encoding": "identity"}
        chunks: list[bytes] = []
        total = 0
        async with self._client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 403:
                raise AudioUrlExpiredError(
                    f"presigned S3 URL returned 403 (expired): {url[:200]}"
                )
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ComulyticError(
                    f"GET {url[:120]}... -> {resp.status_code}: {body[:300]!r}"
                )
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ComulyticError(
                        f"presigned audio exceeded {max_bytes} byte cap (got {total})"
                    )
                chunks.append(chunk)
        return b"".join(chunks)


# ---------------------------------------------------------------------------
# Rate-limit retry helpers (module-level so they're cheap + testable)
# ---------------------------------------------------------------------------


def _retry_sleep(resp: httpx.Response | None) -> float:
    """Compute a retry sleep duration from a rate-limited response.

    Prefers `Retry-After` (seconds), then `x-ratelimit-reset` (seconds until
    reset), then a default of 1.0s. Returns at most 60s to avoid long stalls.
    """
    if resp is None:
        return 1.0
    ra = resp.headers.get("Retry-After")
    if ra is not None:
        try:
            return min(float(ra), 60.0)
        except ValueError:
            pass
    reset_ = resp.headers.get("x-ratelimit-reset")
    if reset_ is not None:
        try:
            r = float(reset_)
            if r > 0:
                return min(r, 60.0)
        except ValueError:
            pass
    return 1.0


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(max(0.0, seconds))
