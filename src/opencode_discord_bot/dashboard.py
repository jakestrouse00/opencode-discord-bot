"""Token-gated ops dashboard for the opencode-discord-bot deployment.

A small Starlette app served in-process by uvicorn (started alongside the
bot from ``OpencodeBot.on_connect`` when ``DASHBOARD_ENABLED=true`` AND a
non-empty ``DASHBOARD_TOKEN`` are set — an empty token means the dashboard
NEVER starts, so local default behavior is unchanged).

Two tabs in one self-contained HTML page:

* **Stats** — bridge metrics (processed/skipped/failed, last poll, in-flight),
  system health (uptime, RSS), session bindings (read-only render of both
  router JSON files), and the last ~500 log lines (in-memory ring).
* **Controls** — skip-transcription toggle, bridge pause/resume, live config
  tweaks (poll interval/page size/max duration), seen-set management
  (mark-all-current-as-seen, clear seen-set), and abort-in-flight.

Auth: every request (page + API) must present the shared secret via
``Authorization: Bearer <token>`` or ``?token=<token>`` (constant-time
compare). With no token configured the app answers 401 to everything.

Control semantics (all EPHEMERAL — a restart resets every toggle):

* skip-transcription ON: the bridge marks new audio-delivered recordings as
  seen WITHOUT transcribing/routing them (the accidental-recording kill
  switch). Recordings skipped this way are silently dropped.
* pause: the bridge skips poll cycles entirely — nothing is marked seen,
  so the backlog processes on resume.
* seen-set actions are queued (``dashboard_state.request_action``) and
  applied by the bridge task at the top of its next poll cycle — the bridge
  owns all writes to ``.comulytic-seen.json``.
* abort cancels the in-flight recording task only (never the bridge task).
* live config tweaks mutate the ``config`` singleton in place; a restart
  reverts to ``.env``/secrets values.
"""

from __future__ import annotations

import asyncio
import json
import secrets as py_secrets
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opencode_discord_bot import dashboard_state
from opencode_discord_bot.config import config

# Mirror of the bridge router's persistence filename (string constant, NOT
# imported from bridge.py — the same pattern as commands.py's
# _BRIDGE_SESSIONS_FILE; keep in sync with bridge.py's _BRIDGE_SESSIONS_FILE).
_BOT_SESSIONS_FILE = ".opencode-discord-bot-sessions.json"
_BRIDGE_SESSIONS_FILE = ".opencode-discord-bridge-sessions.json"

_CONTROL_ACTIONS = frozenset(
    {
        "skip_on",
        "skip_off",
        "pause_on",
        "pause_off",
        "abort",
        "mark_all_seen",
        "clear_seen",
    }
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _token_from_request(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.query_params.get("token", ""))


def _authorized(request: Request) -> bool:
    """Constant-time token compare; an empty configured token refuses
    everything (the dashboard must never run unauthenticated)."""
    expected = config.dashboard_token
    if not expected:
        return False
    supplied = _token_from_request(request)
    return py_secrets.compare_digest(supplied.encode(), expected.encode())


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_json_bindings(path: str) -> dict[str, Any]:
    """Read a SessionRouter persistence file read-only (best-effort)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _rss_kb() -> int | None:
    """Current process RSS in KiB from /proc (Linux/Fly); None elsewhere."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _bridge_enabled() -> bool:
    return bool(config.comulytic_enabled and config.comulytic_jwt)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


async def api_stats(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    max_cap = (
        config.comulytic_max_duration_hours * 3600
        + config.comulytic_max_duration_minutes * 60
        + config.comulytic_max_duration_seconds
    )
    payload: dict[str, Any] = {
        "uptime_s": round(dashboard_state.uptime_seconds(), 1),
        "rss_kb": _rss_kb(),
        "bridge": {
            "enabled": _bridge_enabled(),
            **dashboard_state.snapshot(),
        },
        "config": {
            "comulytic_poll_interval_seconds": config.comulytic_poll_interval_seconds,
            "comulytic_poll_page_size": config.comulytic_poll_page_size,
            "comulytic_max_duration_hours": config.comulytic_max_duration_hours,
            "comulytic_max_duration_minutes": config.comulytic_max_duration_minutes,
            "comulytic_max_duration_seconds": config.comulytic_max_duration_seconds,
            "comulytic_max_duration_cap_s": max_cap,
        },
        "sessions": {
            "bot": _read_json_bindings(_BOT_SESSIONS_FILE),
            "bridge": _read_json_bindings(_BRIDGE_SESSIONS_FILE),
        },
    }
    return JSONResponse(payload)


async def api_logs(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    return JSONResponse({"entries": dashboard_state.log_entries()})


async def api_control(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    action = str(body.get("action", ""))
    if action not in _CONTROL_ACTIONS:
        return JSONResponse(
            {"error": f"unknown action {action!r}"}, status_code=400
        )
    if action == "skip_on":
        dashboard_state.set_skip_transcription(True)
    elif action == "skip_off":
        dashboard_state.set_skip_transcription(False)
    elif action == "pause_on":
        dashboard_state.set_paused(True)
    elif action == "pause_off":
        dashboard_state.set_paused(False)
    elif action == "abort":
        aborted = dashboard_state.abort_in_flight()
        return JSONResponse({"action": action, "aborted": aborted})
    elif action == "mark_all_seen":
        dashboard_state.request_action("mark_all_seen")
    elif action == "clear_seen":
        dashboard_state.request_action("clear_seen")
    return JSONResponse({"action": action, "ok": True})


async def api_config(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    applied: dict[str, Any] = {}

    if "comulytic_poll_interval_seconds" in body:
        value = body["comulytic_poll_interval_seconds"]
        if not isinstance(value, (int, float)) or not (0 < float(value) <= 3600):
            return JSONResponse(
                {"error": "comulytic_poll_interval_seconds must be 0 < n <= 3600"},
                status_code=400,
            )
        config.comulytic_poll_interval_seconds = float(value)
        applied["comulytic_poll_interval_seconds"] = float(value)

    if "comulytic_poll_page_size" in body:
        value = body["comulytic_poll_page_size"]
        if not isinstance(value, int) or not (1 <= value <= 100):
            return JSONResponse(
                {"error": "comulytic_poll_page_size must be an int in 1..100"},
                status_code=400,
            )
        config.comulytic_poll_page_size = value
        applied["comulytic_poll_page_size"] = value

    for field, lo, hi in (
        ("comulytic_max_duration_hours", 0, 24),
        ("comulytic_max_duration_minutes", 0, 59),
        ("comulytic_max_duration_seconds", 0, 59),
    ):
        if field in body:
            value = body[field]
            if not isinstance(value, int) or not (lo <= value <= hi) or value < 0:
                return JSONResponse(
                    {"error": f"{field} must be an int >= {lo}"},
                    status_code=400,
                )
            setattr(config, field, value)
            applied[field] = value

    return JSONResponse({"ok": True, "applied": applied})


# ---------------------------------------------------------------------------
# HTML page (self-contained; the token lives in the URL query and is copied
# into the fetch calls client-side)
# ---------------------------------------------------------------------------

_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>opencode-discord-bot dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background: #14161a; color: #d7dce3;
         margin: 0; padding: 1rem; }
  h1 { font-size: 1.2rem; margin: 0 0 1rem; }
  .tabs { display: flex; gap: .5rem; margin-bottom: 1rem; }
  .tabs button { padding: .45rem 1rem; border: 1px solid #333a44; border-radius: 6px;
                 background: #1d2127; color: #d7dce3; cursor: pointer; }
  .tabs button.active { background: #2d5aa8; border-color: #2d5aa8; }
  .card { background: #1d2127; border: 1px solid #333a44; border-radius: 8px;
          padding: .9rem 1rem; margin-bottom: 1rem; }
  .card h2 { font-size: .95rem; margin: 0 0 .6rem; color: #9fb4d8; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: .7rem; }
  .stat { background: #14161a; border-radius: 6px; padding: .55rem .7rem; }
  .stat .k { font-size: .72rem; text-transform: uppercase; color: #7d8794; }
  .stat .v { font-size: 1.15rem; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; font-size: .8rem; }
  td, th { text-align: left; padding: .25rem .4rem; border-bottom: 1px solid #262b33; }
  .logs { max-height: 420px; overflow-y: auto; font-family: ui-monospace, monospace;
          font-size: .72rem; white-space: pre-wrap; }
  .log-line { padding: 1px 0; border-bottom: 1px solid #1a1e24; }
  .lvl-ERROR { color: #ff7b72; } .lvl-WARNING { color: #e3b341; }
  .lvl-INFO { color: #79c0ff; } .lvl-DEBUG { color: #6e7681; }
  .controls { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }
  .btn { padding: .5rem .9rem; border-radius: 6px; border: 1px solid #333a44;
         background: #262b33; color: #d7dce3; cursor: pointer; font-size: .85rem; }
  .btn.danger { background: #5c2323; border-color: #8a3a3a; }
  .btn.on { background: #2d5aa8; border-color: #4a7fd6; }
  .toggle-note { font-size: .75rem; color: #7d8794; margin-left: .4rem; }
  label { font-size: .8rem; color: #9fb4d8; display: block; margin-bottom: .2rem; }
  input { background: #14161a; border: 1px solid #333a44; color: #d7dce3;
          border-radius: 5px; padding: .35rem .5rem; width: 130px; }
  .cfg-row { display: flex; flex-wrap: wrap; gap: .9rem; margin-bottom: .6rem; }
  .err { color: #ff7b72; font-size: .8rem; margin-top: .4rem; }
  #refresh-ts { font-size: .72rem; color: #6e7681; }
</style>
</head>
<body>
<h1>opencode-discord-bot dashboard</h1>
<div class="tabs">
  <button id="tab-stats" class="active" onclick="switchTab('stats')">Stats</button>
  <button id="tab-controls" onclick="switchTab('controls')">Controls</button>
</div>

<div id="pane-stats">
  <div class="card"><h2>Bridge</h2>
    <div class="grid" id="bridge-stats"></div>
    <div class="toggle-note" id="bridge-flags"></div>
  </div>
  <div class="card"><h2>System</h2>
    <div class="grid" id="sys-stats"></div>
  </div>
  <div class="card"><h2>Session bindings</h2>
    <div id="bindings"></div>
  </div>
  <div class="card"><h2>Recent recordings</h2>
    <table id="recent-table"><thead>
      <tr><th>note id</th><th>status</th><th>seconds</th><th>at</th></tr>
    </thead><tbody></tbody></table>
  </div>
  <div class="card"><h2>Logs (last 500)</h2>
    <div class="logs" id="logs"></div>
  </div>
  <p id="refresh-ts"></p>
</div>

<div id="pane-controls" style="display:none">
  <div class="card"><h2>Skip transcription</h2>
    <div class="controls">
      <button class="btn" id="btn-skip" onclick="act('skip_on')">Skip transcription: ON</button>
      <button class="btn" id="btn-skip-off" onclick="act('skip_off')">Skip transcription: OFF</button>
    </div>
    <div class="toggle-note">ON = every NEW recording from Comulytic is marked
      processed (seen) but never transcribed or routed to Bobby. Use for
      accidental recordings. Resets to OFF on restart.</div>
  </div>
  <div class="card"><h2>Bridge pause / resume</h2>
    <div class="controls">
      <button class="btn" id="btn-pause" onclick="act('pause_on')">Pause polling</button>
      <button class="btn" id="btn-resume" onclick="act('pause_off')">Resume polling</button>
    </div>
    <div class="toggle-note">Paused = poll cycles skipped entirely; nothing is
      marked seen, so the backlog processes on resume. (Different from skip:
      skip DROPS recordings silently; pause HOLDS them.)</div>
  </div>
  <div class="card"><h2>Abort in-flight processing</h2>
    <div class="controls">
      <button class="btn danger" onclick="act('abort')">Abort current recording</button>
    </div>
    <div class="toggle-note">Cancels the recording currently being transcribed.
      It is already marked seen, so it will NOT reprocess.</div>
  </div>
  <div class="card"><h2>Seen-set management</h2>
    <div class="controls">
      <button class="btn" onclick="act('mark_all_seen')">Mark ALL current recordings as seen</button>
      <button class="btn danger" onclick="act('clear_seen')">Clear seen-set</button>
    </div>
    <div class="toggle-note">Mark-all = bulk-skip everything currently on
      Comulytic (applied at the next poll cycle). Clear = EVERYTHING
      reprocesses on the next cycle. Clear is destructive.</div>
  </div>
  <div class="card"><h2>Live config (reverts on restart)</h2>
    <div class="cfg-row">
      <div><label>Poll interval (s)</label>
        <input id="cfg-interval" type="number" min="1" max="3600" step="1"></div>
      <div><label>Max duration (h)</label>
        <input id="cfg-h" type="number" min="0" max="24" step="1"></div>
      <div><label>Max duration (m)</label>
        <input id="cfg-m" type="number" min="0" max="59" step="1"></div>
      <div><label>Max duration (s)</label>
        <input id="cfg-s" type="number" min="0" max="59" step="1"></div>
      <div><label>Page size</label>
        <input id="cfg-page" type="number" min="1" max="100" step="1"></div>
      <button class="btn" onclick="applyConfig()">Apply</button>
    </div>
    <div class="toggle-note">Applied to the running process immediately.
      A restart reverts to .env / Fly secrets values.</div>
    <div class="err" id="cfg-err"></div>
  </div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
async function api(path, opts) {
  const r = await fetch(path + "?token=" + encodeURIComponent(TOKEN), opts);
  if (r.status === 401) { document.body.innerHTML =
      "<p>Unauthorized. Append ?token=&lt;DASHBOARD_TOKEN&gt; to the URL.</p>"; throw 0; }
  return r;
}
function esc(s) { const d = document.createElement("div"); d.textContent = String(s);
                  return d.innerHTML; }
function stat(k, v) { return `<div class="stat"><div class="k">${esc(k)}</div>
  <div class="v">${esc(v)}</div></div>`; }
function tsFmt(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : "never"; }

async function refresh() {
  try {
    const [statsR, logsR] = await Promise.all([api("/api/stats"), api("/api/logs")]);
    const s = await statsR.json(), l = await logsR.json();
    const b = s.bridge;
    document.getElementById("bridge-stats").innerHTML =
      stat("processed", b.processed) + stat("skipped", b.skipped) +
      stat("failed", b.failed) + stat("in flight", b.in_flight_note_id || "—") +
      stat("seen", b.seen_count ?? "—") + stat("last poll", tsFmt(b.last_poll_at)) +
      stat("poll status", b.last_poll_status || "—");
    document.getElementById("bridge-flags").textContent =
      `skip-transcription: ${b.skip_transcription ? "ON" : "off"}  |  ` +
      `paused: ${b.paused ? "PAUSED" : "running"}  |  bridge: ` +
      `${b.enabled ? "enabled" : "disabled"}` +
      (b.pending_action ? `  |  pending: ${b.pending_action}` : "");
    document.getElementById("sys-stats").innerHTML =
      stat("uptime", Math.floor(s.uptime_s / 60) + "m " + Math.floor(s.uptime_s % 60) + "s") +
      stat("RSS", s.rss_kb ? (s.rss_kb / 1024).toFixed(0) + " MiB" : "n/a");
    document.getElementById("btn-skip").classList.toggle("on", b.skip_transcription);
    document.getElementById("btn-pause").classList.toggle("on", b.paused);

    const rows = (b.recent || []).map(r =>
      `<tr><td>${esc(r.note_id)}</td><td>${esc(r.status)}</td><td>${esc(r.seconds)}</td>
       <td>${esc(new Date(r.at * 1000).toLocaleTimeString())}</td></tr>`).join("");
    document.querySelector("#recent-table tbody").innerHTML =
      rows || "<tr><td colspan=4>no recordings yet</td></tr>";

    const sess = s.sessions;
    const sessHtml = (title, map) =>
      `<div><h2 style="font-size:.8rem;color:#9fb4d8;margin:.4rem 0">${esc(title)} (${Object.keys(map).length})</h2>` +
      Object.entries(map).map(([ch, sid]) =>
        `<div style="font-size:.75rem"><code>${esc(ch)}</code> &rarr; <code>${esc(sid)}</code></div>`
      ).join("") + "</div>";
    document.getElementById("bindings").innerHTML =
      sessHtml("bot router", sess.bot || {}) + sessHtml("bridge router", sess.bridge || {});

    document.getElementById("logs").innerHTML = (l.entries || []).map(e =>
      `<div class="log-line lvl-${esc(e.level)}">[${esc(tsFmt(e.ts))}] ${esc(e.level)} ${esc(e.name)}: ${esc(e.message)}</div>`
    ).join("");
    const el = document.getElementById("logs"); el.scrollTop = el.scrollHeight;

    if (document.activeElement.id !== "cfg-interval")
      document.getElementById("cfg-interval").value = s.config.comulytic_poll_interval_seconds;
    if (document.activeElement.id !== "cfg-page")
      document.getElementById("cfg-page").value = s.config.comulytic_poll_page_size;
    if (!document.activeElement.id.startsWith("cfg-") ||
        document.activeElement.id === "cfg-h")
      document.getElementById("cfg-h").value = s.config.comulytic_max_duration_hours;
    if (document.activeElement.id === "cfg-m")
      document.getElementById("cfg-m").value = s.config.comulytic_max_duration_minutes;
    if (document.activeElement.id === "cfg-s")
      document.getElementById("cfg-s").value = s.config.comulytic_max_duration_seconds;

    document.getElementById("refresh-ts").textContent =
      "refreshed " + new Date().toLocaleTimeString();
  } catch (e) { if (e !== 0) console.error(e); }
}

async function act(action) {
  const r = await api("/api/control", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action}),
  });
  const j = await r.json();
  if (!r.ok) alert("error: " + (j.error || r.status));
  if (action === "clear_seen" && !confirm("Clearing the seen-set means EVERY " +
      "recording on Comulytic will reprocess (transcribe + route) on the next " +
      "poll cycle. The mark-all button you just pressed was NOT confirmed. " +
      "Press OK on the NEXT dialog to actually clear.")) { return; }
  refresh();
}

async function applyConfig() {
  document.getElementById("cfg-err").textContent = "";
  const body = {
    comulytic_poll_interval_seconds:
      parseFloat(document.getElementById("cfg-interval").value),
    comulytic_poll_page_size: parseInt(document.getElementById("cfg-page").value),
    comulytic_max_duration_hours: parseInt(document.getElementById("cfg-h").value || "0"),
    comulytic_max_duration_minutes: parseInt(document.getElementById("cfg-m").value || "0"),
    comulytic_max_duration_seconds: parseInt(document.getElementById("cfg-s").value || "0"),
  };
  const r = await api("/api/config", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!r.ok) { const j = await r.json();
    document.getElementById("cfg-err").textContent = j.error || "error " + r.status; }
  refresh();
}

function switchTab(tab) {
  document.getElementById("tab-stats").classList.toggle("active", tab === "stats");
  document.getElementById("tab-controls").classList.toggle("active", tab === "controls");
  document.getElementById("pane-stats").style.display = tab === "stats" ? "" : "none";
  document.getElementById("pane-controls").style.display = tab === "controls" ? "" : "none";
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


async def index(request: Request) -> Response:
    if not _authorized(request):
        return _unauthorized()
    return Response(_PAGE_HTML, media_type="text/html")


def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/api/stats", api_stats, methods=["GET"]),
            Route("/api/logs", api_logs, methods=["GET"]),
            Route("/api/control", api_control, methods=["POST"]),
            Route("/api/config", api_config, methods=["POST"]),
        ]
    )


# ---------------------------------------------------------------------------
# Lifecycle (called from OpencodeBot.on_connect / OpencodeBot.close)
# ---------------------------------------------------------------------------

_server_task: asyncio.Task[None] | None = None


def start_dashboard() -> None:
    """Start the dashboard server as an in-process asyncio task.

    Idempotent (module guard) and a silent no-op unless
    ``config.dashboard_enabled`` AND ``config.dashboard_token`` are both
    set — an empty token must never yield an unauthenticated dashboard.
    """
    global _server_task
    if _server_task is not None:
        return
    if not config.dashboard_enabled or not config.dashboard_token:
        return

    import logging

    import uvicorn

    dashboard_state.install_log_handler()
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=config.dashboard_port,
            log_level="warning",
        )
    )

    async def _serve() -> None:
        logging.getLogger(__name__).info(
            "dashboard listening on 0.0.0.0:%d (token-gated)",
            config.dashboard_port,
        )
        await server.serve()

    _server_task = asyncio.create_task(_serve())


def stop_dashboard() -> None:
    """Cancel + drain the dashboard server task (best-effort, never raises)."""
    global _server_task
    task = _server_task
    _server_task = None
    dashboard_state.remove_log_handler()
    if task is None:
        return
    task.cancel()