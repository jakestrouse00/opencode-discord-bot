"""Unit tests for ``dashboard.py`` — auth, endpoints, control + config APIs.

Uses ``starlette.testclient.TestClient`` (transport = httpx, already a dep
of the package). No uvicorn server, no network, no gateway. The config
singleton is monkeypatched per-test; ``dashboard_state`` is reset between
tests.
"""

import logging

import pytest
from starlette.testclient import TestClient

from opencode_discord_bot import dashboard as dash
from opencode_discord_bot import dashboard_state as ds
from opencode_discord_bot.config import config


@pytest.fixture(autouse=True)
def _reset_state():
    ds.reset()
    yield
    ds.reset()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "dashboard_token", "tok")
    monkeypatch.setattr(config, "dashboard_enabled", True)
    app = dash.create_app()
    with TestClient(app) as c:
        yield c


def test_empty_token_refuses_everything(monkeypatch):
    monkeypatch.setattr(config, "dashboard_token", "")
    app = dash.create_app()
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/api/stats").status_code == 401


def test_no_token_401(client):
    assert client.get("/api/stats").status_code == 401


def test_wrong_token_401(client):
    assert client.get("/api/stats?token=wrong").status_code == 401
    assert client.get("/api/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_bearer_and_query_token_both_accepted(client):
    assert client.get("/api/stats?token=tok").status_code == 200
    assert client.get("/api/stats", headers={"Authorization": "Bearer tok"}).status_code == 200


def test_index_serves_html(client):
    resp = client.get("/?token=tok")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "dashboard" in resp.text


def test_stats_payload_shape(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # router JSON files read from cwd
    resp = client.get("/api/stats?token=tok")
    assert resp.status_code == 200
    body = resp.json()
    assert "uptime_s" in body
    assert "rss_kb" in body
    b = body["bridge"]
    for key in (
        "skip_transcription",
        "paused",
        "processed",
        "skipped",
        "failed",
        "last_poll_at",
        "last_poll_status",
        "in_flight_note_id",
        "seen_count",
        "recent",
    ):
        assert key in b
    assert "config" in body
    assert "comulytic_poll_interval_seconds" in body["config"]
    assert "sessions" in body
    assert body["sessions"]["bot"] == {}
    assert body["sessions"]["bridge"] == {}


def test_control_skip_toggle(client):
    r = client.post("/api/control?token=tok", json={"action": "skip_on"})
    assert r.status_code == 200
    assert ds.is_skipping_transcription() is True
    client.post("/api/control?token=tok", json={"action": "skip_off"})
    assert ds.is_skipping_transcription() is False


def test_control_pause_toggle(client):
    client.post("/api/control?token=tok", json={"action": "pause_on"})
    assert ds.is_paused() is True
    client.post("/api/control?token=tok", json={"action": "pause_off"})
    assert ds.is_paused() is False


def test_control_seen_actions_queue_pending(client):
    client.post("/api/control?token=tok", json={"action": "mark_all_seen"})
    assert ds.take_pending_action() == "mark_all_seen"
    client.post("/api/control?token=tok", json={"action": "clear_seen"})
    assert ds.take_pending_action() == "clear_seen"


def test_control_abort_no_in_flight(client):
    r = client.post("/api/control?token=tok", json={"action": "abort"})
    assert r.status_code == 200
    assert r.json()["aborted"] is False


def test_control_unknown_action_400(client):
    r = client.post("/api/control?token=tok", json={"action": "explode"})
    assert r.status_code == 400


def test_control_requires_auth(client):
    r = client.post("/api/control", json={"action": "skip_on"})
    assert r.status_code == 401
    assert ds.is_skipping_transcription() is False


def test_config_update_applies(client, monkeypatch):
    monkeypatch.setattr(config, "comulytic_poll_interval_seconds", 60.0)
    r = client.post(
        "/api/config?token=tok",
        json={
            "comulytic_poll_interval_seconds": 15,
            "comulytic_poll_page_size": 50,
            "comulytic_max_duration_hours": 2,
            "comulytic_max_duration_minutes": 30,
            "comulytic_max_duration_seconds": 0,
        },
    )
    assert r.status_code == 200
    assert config.comulytic_poll_interval_seconds == 15.0
    assert config.comulytic_poll_page_size == 50
    assert config.comulytic_max_duration_hours == 2
    assert config.comulytic_max_duration_minutes == 30


def test_config_rejects_invalid(client, monkeypatch):
    monkeypatch.setattr(config, "comulytic_poll_interval_seconds", 60.0)
    r = client.post("/api/config?token=tok", json={"comulytic_poll_interval_seconds": -5})
    assert r.status_code == 400
    assert config.comulytic_poll_interval_seconds == 60.0
    r = client.post("/api/config?token=tok", json={"comulytic_poll_page_size": 0})
    assert r.status_code == 400
    r = client.post("/api/config?token=tok", json={"comulytic_max_duration_hours": -1})
    assert r.status_code == 400


def test_config_requires_auth(client):
    r = client.post("/api/config", json={"comulytic_poll_interval_seconds": 15})
    assert r.status_code == 401


def test_logs_endpoint(client):
    ds.install_log_handler()
    logging.getLogger("test.dash").warning("captured line")
    r = client.get("/api/logs?token=tok")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert any(e["message"] == "captured line" for e in entries)


def test_start_dashboard_gates(monkeypatch):
    # Empty token -> never starts.
    monkeypatch.setattr(config, "dashboard_enabled", True)
    monkeypatch.setattr(config, "dashboard_token", "")
    dash._server_task = None
    dash.start_dashboard()
    assert dash._server_task is None
    # Disabled -> never starts.
    monkeypatch.setattr(config, "dashboard_enabled", False)
    monkeypatch.setattr(config, "dashboard_token", "tok")
    dash.start_dashboard()
    assert dash._server_task is None


def test_session_bindings_read_from_disk(client, monkeypatch, tmp_path):
    (tmp_path / ".opencode-discord-bot-sessions.json").write_text(
        '{"123": "ses-bot-1"}'
    )
    (tmp_path / ".opencode-discord-bridge-sessions.json").write_text(
        '{"456": "ses-bridge-1"}'
    )
    monkeypatch.chdir(tmp_path)
    body = client.get("/api/stats?token=tok").json()
    assert body["sessions"]["bot"] == {"123": "ses-bot-1"}
    assert body["sessions"]["bridge"] == {"456": "ses-bridge-1"}