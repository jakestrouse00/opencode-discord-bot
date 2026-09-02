"""Unit tests for ``comulytic.py`` — pure helpers + ``ComulyticClient`` via MockTransport."""

import base64
import datetime as _dt
import json

import httpx
import pytest

from opencode_discord_bot.comulytic import (
    ComulyticClient,
    ComulyticError,
    AudioUrlExpiredError,
    jwt_expiry,
    is_audio_delivered,
    parse_audio_url_expiry,
    _retry_sleep,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _make_jwt(exp: int) -> str:
    """Build a fake HS256-shaped JWT with the given ``exp`` (unix seconds)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    sig = "sig"
    return f"{header}.{payload}.{sig}"


def test_jwt_expiry_decodes_exp():
    exp = int(_dt.datetime(2026, 12, 31, tzinfo=_dt.timezone.utc).timestamp())
    jwt = _make_jwt(exp)
    result = jwt_expiry(jwt)
    assert int(result.timestamp()) == exp


def test_jwt_expiry_malformed_too_few_segments():
    with pytest.raises(ComulyticError, match="segments"):
        jwt_expiry("onlyone")  # 1 segment — no dot at all


def test_jwt_expiry_malformed_payload():
    with pytest.raises(ComulyticError, match="payload"):
        jwt_expiry("a.@@@.c")


def test_jwt_expiry_missing_exp():
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(b'{"no":"exp"}').rstrip(b"=").decode()
    with pytest.raises(ComulyticError, match="exp"):
        jwt_expiry(f"{header}.{payload}.sig")


def test_is_audio_delivered_true_when_cloud_and_public():
    assert is_audio_delivered({"hasCloudAudio": True, "audioAccess": "public"}) is True


def test_is_audio_delivered_false_when_no_cloud():
    assert is_audio_delivered({"hasCloudAudio": False, "audioAccess": "public"}) is False


def test_is_audio_delivered_false_when_not_public():
    assert is_audio_delivered({"hasCloudAudio": True, "audioAccess": "private"}) is False


def test_is_audio_delivered_false_when_missing():
    assert is_audio_delivered({}) is False


def test_parse_audio_url_expiry_parses_amz_params():
    url = (
        "https://s3.example.com/audio.mp3"
        "?X-Amz-Date=20260101T120000Z&X-Amz-Expires=172800"
    )
    result = parse_audio_url_expiry(url)
    assert result is not None
    expected = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    expected += _dt.timedelta(seconds=172800)
    assert result == expected


def test_parse_audio_url_expiry_none_when_no_amz_params():
    assert parse_audio_url_expiry("https://example.com/x") is None


def test_parse_audio_url_expiry_none_on_garbage():
    assert parse_audio_url_expiry("https://example.com/?X-Amz-Date=garbage&X-Amz-Expires=1") is None


def test_retry_sleep_no_resp_returns_default():
    assert _retry_sleep(None) == 1.0


def test_retry_sleep_uses_retry_after(monkeypatch):
    resp = httpx.Response(200, headers={"Retry-After": "5"})
    assert _retry_sleep(resp) == 5.0


def test_retry_sleep_caps_at_60():
    resp = httpx.Response(200, headers={"Retry-After": "999"})
    assert _retry_sleep(resp) == 60.0


def test_retry_sleep_uses_ratelimit_reset():
    resp = httpx.Response(200, headers={"x-ratelimit-reset": "10"})
    assert _retry_sleep(resp) == 10.0


def test_retry_sleep_invalid_retry_after_falls_through():
    resp = httpx.Response(200, headers={"Retry-After": "abc", "x-ratelimit-reset": "3"})
    assert _retry_sleep(resp) == 3.0


def test_get_audio_url_returns_raw_and_enhanced():
    detail = {
        "audioUrl": "https://enhanced.mp3",
        "objStorageUrl": "https://raw.mp3",
        "audioUrlVO": {"rawAudio": "https://vo-raw.mp3", "enhanceAudio": "https://vo-enh.mp3"},
    }
    enhanced, raw = ComulyticClient.get_audio_url(detail)
    assert enhanced == "https://enhanced.mp3"
    assert raw == "https://raw.mp3"


def test_get_audio_url_falls_through_to_audio_vo():
    detail = {"audioUrlVO": {"rawAudio": "https://vo-raw.mp3", "enhanceAudio": "https://vo-enh.mp3"}}
    enhanced, raw = ComulyticClient.get_audio_url(detail)
    assert enhanced == "https://vo-enh.mp3"
    assert raw == "https://vo-raw.mp3"


def test_get_audio_url_returns_none_when_absent():
    enhanced, raw = ComulyticClient.get_audio_url({})
    assert enhanced is None
    assert raw is None


# ---------------------------------------------------------------------------
# ComulyticClient (httpx via MockTransport)
# ---------------------------------------------------------------------------


def test_construct_raises_on_empty_jwt():
    with pytest.raises(ComulyticError, match="jwt is empty"):
        ComulyticClient("https://api.test", jwt="", user_agent="ua")


async def test_list_recordings_posts_paging():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"data": {"data": [], "total": 0}, "success": True})

    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua", web_base="https://web.test")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    try:
        await c.list_recordings(page=2, page_size=10)
        assert captured["path"] == "/api/kirby/v2/note/paging"
        assert captured["method"] == "POST"
        assert captured["body"]["page"] == 2
        assert captured["body"]["pageSize"] == 10
    finally:
        await c.close()


async def test_probe_newest_returns_total_and_id():
    def handler(request):
        return httpx.Response(200, json={
            "data": {"data": [{"noteId": "abc"}], "total": 1}, "success": True
        })
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    try:
        total, newest = await c.probe_newest()
        assert total == 1
        assert newest == "abc"
    finally:
        await c.close()


async def test_probe_newest_empty_returns_zero_none():
    def handler(request):
        return httpx.Response(200, json={"data": {"data": [], "total": 0}, "success": True})
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    try:
        total, newest = await c.probe_newest()
        assert total == 0
        assert newest is None
    finally:
        await c.close()


async def test_get_note_detail_returns_data_block():
    def handler(request):
        return httpx.Response(200, json={
            "data": {"audioUrl": "https://x", "hasCloudAudio": True}, "success": True
        })
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    try:
        result = await c.get_note_detail("n-1")
        assert result["hasCloudAudio"] is True
    finally:
        await c.close()


async def test_download_audio_proxy_streams_bytes():
    def handler(request):
        return httpx.Response(206, content=b"audio-bytes-12345")
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua", web_base="https://web.test")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://web.test")
    try:
        result = await c.download_audio_proxy("n-1")
        assert result == b"audio-bytes-12345"
    finally:
        await c.close()


async def test_download_audio_presigned_403_raises_expired():
    def handler(request):
        return httpx.Response(403, text="expired")
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua", web_base="https://web.test")
    c._s3_client = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(AudioUrlExpiredError):
            await c.download_audio_presigned("https://s3.example/x?sig=abc")
    finally:
        await c.close()


async def test_download_audio_proxy_4xx_raises_comulytic_error():
    def handler(request):
        return httpx.Response(404, text="not found")
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua", web_base="https://web.test")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://web.test")
    try:
        with pytest.raises(ComulyticError, match="404"):
            await c.download_audio_proxy("n-1")
    finally:
        await c.close()


async def test_download_audio_proxy_follows_cross_host_redirect_with_auth():
    """Path B host migration (web.comulytic.ai → web.comu.com): the 301 must
    be followed with the auth cookie + Authorization header RE-SENT on the
    new host (httpx's follow_redirects would strip the header and leave the
    domain-scoped cookie behind → 401 on every recording)."""
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append((request.url.host, request.headers.get("authorization"),
                     request.headers.get("cookie")))
        if request.url.host == "old.test":
            return httpx.Response(
                301,
                headers={"Location": "https://new.test/api/note/audio-range/n-1"},
            )
        return httpx.Response(206, content=b"audio-bytes-12345")

    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua",
                        web_base="https://old.test")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://old.test")
    try:
        result = await c.download_audio_proxy("n-1")
        assert result == b"audio-bytes-12345"
        # Two hops: old host (301) then new host (206).
        assert [h[0] for h in hops] == ["old.test", "new.test"]
        # Auth header AND cookie survive the cross-host redirect.
        for host, auth, cookie in hops:
            assert auth == "Bearer jwt", f"Authorization header missing on {host}"
            assert cookie and "authorization=Bearer%20jwt" in cookie, (
                f"auth cookie missing on {host}"
            )
    finally:
        await c.close()


async def test_download_audio_proxy_redirect_without_location_raises():
    def handler(request):
        return httpx.Response(301)  # no Location header
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua", web_base="https://web.test")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://web.test")
    try:
        with pytest.raises(ComulyticError, match="without Location"):
            await c.download_audio_proxy("n-1")
    finally:
        await c.close()


async def test_download_audio_presigned_sends_no_authorization_header():
    """Path A must go through the auth-free `_s3_client`: S3 rejects requests
    presenting both the query-string signature and an Authorization header
    (400 InvalidArgument "Only one auth mechanism allowed")."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["cookie"] = request.headers.get("cookie")
        return httpx.Response(206, content=b"s3-bytes")

    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua", web_base="https://web.test")
    # Route BOTH clients through the transport so a header leak would show up.
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    c._s3_client = httpx.AsyncClient(transport=transport)
    try:
        result = await c.download_audio_presigned(
            "https://s3.example/audio/x.mp3?X-Amz-Signature=abc"
        )
        assert result == b"s3-bytes"
        assert captured["authorization"] is None, (
            "Authorization header leaked onto the pre-signed S3 GET"
        )
        assert captured["cookie"] is None, (
            "auth cookie leaked onto the pre-signed S3 GET"
        )
    finally:
        await c.close()


async def test_non_json_response_raises_comulytic_error():
    def handler(request):
        return httpx.Response(200, text="<html>WAF interstitial</html>",
                              headers={"content-type": "text/html"})
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    try:
        with pytest.raises(ComulyticError, match="non-JSON"):
            await c.list_recordings()
    finally:
        await c.close()


async def test_success_false_raises_comulytic_error():
    def handler(request):
        return httpx.Response(200, json={"success": False, "msg": "rate limited"})
    transport = httpx.MockTransport(handler)
    c = ComulyticClient("https://api.test", jwt="jwt", user_agent="ua")
    c._client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    try:
        with pytest.raises(ComulyticError, match="rate limited"):
            await c.list_recordings()
    finally:
        await c.close()