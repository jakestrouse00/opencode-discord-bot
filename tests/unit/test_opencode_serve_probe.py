"""Unit tests for ``opencode_serve.OpencodeServe`` health-probe body validation.

The probe must reject a 200 + HTML response (the ``opencode-remote-gui`` Flet
catch-all on port 4096) so the bot doesn't false-positive "reuse" the GUI as
opencode serve — the direct cause of the ``POST /session -> 405`` error.
These tests monkeypatch ``urllib.request.urlopen`` so no real server is
spawned (the suite never launches ``opencode serve`` per AGENTS.md).
"""

import json
import urllib.error
import urllib.request

from opencode_discord_bot.opencode_serve import OpencodeServe


class _FakeResponse:
    """Minimal stand-in for the ``http.client.HTTPResponse`` returned by
    ``urllib.request.urlopen``: supports ``.status``, ``.read(n)``, and the
    context-manager protocol (``__enter__``/``__exit__``)."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self._read = False

    def read(self, amt: int | None = None) -> bytes:
        if self._read:
            return b""
        self._read = True
        if amt is not None:
            return self._body[:amt]
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_serve() -> OpencodeServe:
    """Construct an OpencodeServe pinned to 4097 with a test password.

    ``__init__`` has no side effects (it only sets attributes; ``_proc`` is
    ``None`` until ``start()`` is called), so this is safe in the test suite.
    """
    return OpencodeServe(port=4097, hostname="127.0.0.1", password="test")


def test_probe_rejects_html_200(monkeypatch):
    """A 200 + HTML body (the Flet GUI catch-all) must NOT be treated as a
    healthy opencode serve — this is the regression that caused the 405."""
    serve = _make_serve()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(
            200, b"<!DOCTYPE html><html><head><title>Flet</title></head></html>"
        ),
    )
    assert serve._probe_healthy() is False


def test_probe_accepts_opencode_json_200(monkeypatch):
    """A 200 + opencode health JSON (``{healthy, version}``) IS healthy."""
    serve = _make_serve()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(
            200, json.dumps({"healthy": True, "version": "1.18.10"}).encode()
        ),
    )
    assert serve._probe_healthy() is True


def test_probe_rejects_401(monkeypatch):
    """A 401 (auth failed / wrong password) is not healthy.

    ``urlopen`` raises ``HTTPError`` for 4xx/5xx (it does NOT return a
    response object), so the probe's ``except`` clause catches it and
    returns False.
    """

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:4097/global/health", 401, "Unauthorized", {}, None
        )

    serve = _make_serve()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert serve._probe_healthy() is False


def test_probe_rejects_non_json_200(monkeypatch):
    """A 200 with a non-JSON body (plain text) is rejected (defensive)."""
    serve = _make_serve()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(200, b"plain text not json"),
    )
    assert serve._probe_healthy() is False


def test_probe_rejects_json_without_health_keys(monkeypatch):
    """A 200 with valid JSON but no ``healthy``/``version`` keys is rejected
    — it's some other JSON server, not opencode."""
    serve = _make_serve()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(
            200, json.dumps({"unrelated": "value"}).encode()
        ),
    )
    assert serve._probe_healthy() is False


def test_wait_healthy_accepts_opencode_json(monkeypatch):
    """``_wait_healthy`` returns True once a 200 + opencode JSON is seen."""
    serve = _make_serve()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(
            200, json.dumps({"healthy": True}).encode()
        ),
    )
    assert serve._wait_healthy(timeout=1.0) is True


def test_wait_healthy_rejects_html_200(monkeypatch):
    """``_wait_healthy`` must NOT return True on a 200 + HTML (the Flet GUI);
    it keeps polling until the timeout elapses, then returns False. Short
    timeout so the 0.25s poll sleep only runs one iteration."""
    serve = _make_serve()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(
            200, b"<!DOCTYPE html><html><head><title>Flet</title></head></html>"
        ),
    )
    assert serve._wait_healthy(timeout=0.1) is False
