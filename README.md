# opencode-discord-bot

A two-way Discord gateway for [opencode](https://opencode.ai). Control
opencode from Discord — run prompts, create plans, record voice, transcribe
audio — all via slash commands. Each `/oc` invocation creates a fresh
opencode session in a fresh Discord channel; subsequent plain-text messages
in that channel are forwarded as follow-up prompts.

> **Not affiliated with the OpenCode team.** This is an independent,
> self-built project and is not built by, endorsed by, or affiliated
> with the [OpenCode](https://opencode.ai) team in any way. It integrates
> with opencode via its public REST API.

## Install via an AI agent

Want an AI agent to install and configure this bot for you? Copy the
prompt below into your LLM (opencode, Claude, ChatGPT, Cursor, etc.). It
walks the agent through the whole setup end-to-end, deferring the manual
Discord Developer Portal steps to you. The steps are host-agnostic — run
the bot locally for a quick start, or adapt them to your container host
of choice (see "Deploy in a container" below).

```
Install and set up the opencode-discord-bot project on my machine from
scratch so I can run the Discord bot immediately with NO further
configuration. This is a two-way Discord gateway for opencode; it uses
Pycord 2.8.1, NOT discord.py. Work through every phase below in order — do
NOT skip a phase or leave a decision unmade. For deep details on any step,
read the named section of README.md / SETUP_GUIDE.md / AGENTS.md in the
cloned repo, but this checklist is the authoritative sequence.

Phase 0 — Environment probe (you run these, report results before continuing):
- OS (Windows / macOS / Linux) and shell.
- `python --version` — must be 3.13+. If older, stop and tell me how to
  install 3.13+ before proceeding.
- `ffmpeg -version` — must be on PATH. If missing, install it
  (`choco install ffmpeg` / `brew install ffmpeg` / `apt install ffmpeg`)
  and re-check.
- `opencode --version` (or `where opencode` / `which opencode`) — must be
  on PATH. If missing, install it (`npm install -g opencode-ai` or from
  https://opencode.ai) and re-check. The bot auto-spawns `opencode serve`.
- GPU probe: run `nvidia-smi`. Record whether an NVIDIA GPU is present
  (yes/no) and, if yes, its VRAM. This drives the CUDA decision in Phase 3
  step 2. Non-NVIDIA GPUs are not supported by faster-whisper's CTranslate2
  backend — treat them as "no GPU".

Phase 1 — Source + install:
- If I don't already have the source:
  `git clone https://github.com/jakestrouse00/opencode-discord-bot.git`,
  then `cd` into that cloned repo directory. (You can also
  `pip install git+https://github.com/jakestrouse00/opencode-discord-bot.git`,
  but clone it so README.md, SETUP_GUIDE.md, and AGENTS.md are on disk.)
- Install the package editable from the clone: `pip install -e .`
  This installs the `opencode_discord_bot` Python package + the
  `opencode-discord-bot` console script. Do NOT install `discord.py`
  alongside it — it conflicts with Pycord.

Phase 2 — Discord Developer Portal (MANUAL — walk me through these with me,
NOT for me; pause after each step so I can do it in my browser):
- Create a new application at https://discord.com/developers/applications
  (or use an existing one).
- Bot tab → Reset Token → copy the token. This is `DISCORD_BOT_TOKEN`.
- Bot tab → Privileged Gateway Intents → enable **Message Content Intent**
  → Save. Without it, plain-text follow-ups silently break.
- OAuth2 → URL Generator → scopes `bot` + `applications.commands` →
  permissions: Send Messages, Manage Channels (it creates session
  channels), Read Message History (edits the "Working…" message), Connect
  + Speak (for /oc_voice). Open the generated URL and invite the bot to
  my server.
- Get my server (guild) id: Discord Developer Mode (User Settings →
  Advanced → Developer Mode), then right-click the server → Copy ID. This
  is `DISCORD_BOT_GUILD_ID`.

Phase 3 — Write `.env` (ask me before each decision, then write the file
at `<repo>/.env`; the package reads `.env` from the cwd at launch). Order:
1. Required keys: `DISCORD_BOT_TOKEN=<token>`,
   `OPENCODE_SERVER_PASSWORD=opencode-local-dev` (any string; basic auth
   for `opencode serve`), `DISCORD_BOT_GUILD_ID=<guild id from Phase 2>`.
2. GPU / CUDA — only if Phase 0 found an NVIDIA GPU: ASK me whether to
   configure the bot to use the GPU. If I say yes:
   - Set `WHISPER_DEVICE=cuda` and `WHISPER_COMPUTE_TYPE=float16` (or
     `int8_float16` if the GPU has low VRAM — ask me).
   - Install CUDA 12 + cuBLAS + cuDNN 9 per SETUP_GUIDE.md "CUDA / GPU
     deps" for my platform (Linux pip wheels + LD_LIBRARY_PATH export;
     Windows: download the NVIDIA-libs archive from
     Purfview/whisper-standalone-win and put the DLLs on PATH).
   - Verify: `python -c "import ctranslate2; print('cuda devices:',
     ctranslate2.get_cuda_device_count())"` must print >0. If it prints
     0, the CUDA libs aren't found — re-check LD_LIBRARY_PATH (Linux) /
     PATH (Windows) before continuing; do NOT leave it at 0.
   If I say no (or no GPU), leave `WHISPER_DEVICE=cpu` and
   `WHISPER_COMPUTE_TYPE=int8` (the defaults).
3. Voice / STT / TTS — ASK: which STT provider? `local` (faster-whisper,
   default, no cloud API, no key) / `openai` (cloud, needs OPENAI_API_KEY)
   / `auto` (local first, cloud fallback). Set `VOICE_STT_PROVIDER`. Then
   ask the local model size if local/auto: `tiny`/`base`/`small`/`medium`
   /`large` → set `VOICE_LOCAL_WHISPER_MODEL` and `WHISPER_MODEL`. Then
   ask whether TTS (bot speaks responses in voice channels) should be
   on. If yes, set `VOICE_TTS_ENABLED=true` and ask for `OPENAI_API_KEY`
   (TTS uses OpenAI's cloud TTS). If no, set `VOICE_TTS_ENABLED=false`
   and leave `OPENAI_API_KEY` empty.
4. Ollama / slug — ASK whether to enable LLM-generated channel-name slugs.
   If yes, ask for `OLLAMA_AUTH_KEY` (get it at https://ollama.com) and
   set `OLLAMA_API_URL=https://ollama.com/v1` (default) and
   `SLUG_MODEL=gpt-oss:20b-cloud` (default). If no, leave
   `OLLAMA_AUTH_KEY` empty — slugs silently degrade to a regex-only
   fallback and the bot still works.
5. Model overrides (optional) — ASK whether to override the opencode model
   per path. If yes, set `OPENCODE_DEFAULT_MODEL=<id>` (for /oc + plain-text
   follow-ups, agent=None) and/or `OPENCODE_ASSISTANT_MODEL=<id>` (for
   /oc_plan, /oc_voice, /oc_talk, voice-message trigger, Comulytic
   bridge; all agent="oc-assistant"). Empty = each opencode agent's own
   frontmatter `model:` wins (the default). Model ids are
   provider-scoped, e.g. `ollama-cloud/glm-5.2`, `anthropic/claude-sonnet-4`.
6. `OPENCODE_SERVE_CWD` — find the project the bot will run against: the
   directory containing the target project's `.opencode/` (plans, agents,
   config). If that directory is the SAME as the repo root you're in,
   leave `OPENCODE_SERVE_CWD` empty (default = bot launch dir). If it's a
   DIFFERENT directory (e.g. the bot's .env lives in a subdirectory but
   the .opencode/ lives at a parent project root), set
   `OPENCODE_SERVE_CWD=<absolute path to that project root>`. Getting this
   wrong causes the "silent plan-loss bug" (sessions resolve to the
   subdir and can't find plans/agents) — see AGENTS.md "OpencodeServe".
7. Comulytic bridge (optional) — ASK whether to enable the Comulytic
   cloud bridge (polls Comulytic for Note Pro recordings, transcribes
   locally via faster-whisper, routes to oc-assistant (Bobby)). If yes, walk me
   through JWT capture per SETUP_GUIDE.md "Comulytic bridge → Capture the
   JWT + refresh token" (sign in at web.comulytic.ai, DevTools Network,
   copy `data.accessToken` and `data.refreshToken` from the login
   response). Set `COMULYTIC_ENABLED=true`,
   `COMULYTIC_JWT=<access token>`,
   `COMULYTIC_REFRESH_TOKEN=<refresh token>`. The bridge auto-spawns
   in-process when the bot starts with these set — no separate launch.
   If no, leave `COMULYTIC_ENABLED` unset/false (silent no-op).

Phase 4 — Install the bundled `oc-assistant (Bobby)` opencode agent into the
TARGET project (the same directory you set as `OPENCODE_SERVE_CWD`, or
the repo root if you left it empty):
`python -m opencode_discord_bot.install_agent --dest <that project root>`
The bot's /oc_plan, /oc_voice, /oc_talk, voice-message trigger, and
Comulytic-bridge paths route to opencode's `oc-assistant (Bobby)` agent, which is
NOT built into opencode — this package ships a generic copy. Without
this install those paths 404. Use `--force` only if overwriting an
existing customized oc-assistant.md is intended.

Phase 5 — Sync slash commands to my guild (one-shot; the bot does NOT
auto-sync on startup):
`python -m opencode_discord_bot.sync_commands --guild <guild id>`
This pushes the slash-command surface — including /oc_setup — to my
guild so they appear in Discord. Run it from the repo root (where .env
lives) so it reads the token + guild id.

Phase 6 — I run the bot + /oc_setup (YOU do NOT run the bot — it
auto-spawns `opencode serve` and outlives your shell, leaving zombie
processes and blocking the port; see AGENTS.md "Do NOT run the bot from
agent context"). Tell me to:
1. Open a separate terminal in the repo root.
2. `python -m opencode_discord_bot` — the gateway starts and
   `opencode serve` is spawned as a child process.
3. In Discord, invoke `/oc_setup` in my server. It creates a category
   "OpenCode Sessions" plus two text channels (`voice-recordings` →
   VOICE_MESSAGE_TRIGGER_CHANNEL_ID, `bot-commands` →
   DISCORD_BOT_ALLOWED_CHANNEL_IDS), writes their IDs + the guild id to
   .env, and reloads config live. Requires the Manage Channels permission.
   Only runs once per guild (refuses if any guild-specific field is
   already set).
4. Stop the bot (Ctrl-C), then re-run
   `python -m opencode_discord_bot.sync_commands --guild <guild id>` so
   every command (now that /oc_setup has written the guild config) is
   registered. Then restart the bot for normal use.
Do NOT leave the bot running in the foreground / blocking your shell.

Phase 7 — Verification (you run these; do not start the gateway):
- `python -c "from opencode_discord_bot.commands import OpencodeBot; print('ok')"`
- `ffmpeg -version`
- `python -c "import faster_whisper; print('ok')"` (skip only if I chose
  cloud-only STT, i.e. VOICE_STT_PROVIDER=openai with no auto fallback).
- Only if WHISPER_DEVICE=cuda:
  `python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"`
  must print >0; 0 = CUDA libs missing (re-check Phase 3 step 2).
- `python -c "from opencode_discord_bot.config import config; print('set' if config.discord_bot_token else 'EMPTY')"`
  must print `set`.
- Then I will do the live smoke test in Discord: `/oc hello` should
  create a channel and respond. (You cannot do this part — it's a live
  gateway interaction.)

Notes:
- /oc_voice recording is currently broken (Pycord 2.8.1 + Discord DAVE
  E2EE on modern voice channels). Mention it but do NOT try to fix it.
  TTS, STT, session-routing, and text commands are unaffected.
- Slash commands are NOT auto-synced on startup (auto_sync_commands=False
  to avoid duplicate UI entries). Sync via `sync_commands --guild <id>`
  after any change to the command surface.
- No tests, linter, or type-checker are configured; the import check
  above is the only automated sanity check.
```

## Quick Start

### 1. Install

```bash
pip install git+https://github.com/jakestrouse00/opencode-discord-bot.git
```

**System dependencies (NOT pip-installable):**
- **Python 3.13+**
- **ffmpeg** — on `PATH` (for voice audio extraction + TTS playback)
- **opencode** — on `PATH` (the bot auto-spawns `opencode serve` on login)

Install ffmpeg via `choco install ffmpeg` (Windows), `brew install ffmpeg`
(macOS), or `apt install ffmpeg` (Linux). Install opencode via
`npm install -g opencode-ai` or from https://opencode.ai.

### 2. Configure

Create a `.env` file in the directory you'll run the bot from (or set env
vars directly):

```env
# Required
DISCORD_BOT_TOKEN=<your bot token from https://discord.com/developers/applications>
OPENCODE_SERVER_PASSWORD=opencode-local-dev

# Optional
DISCORD_BOT_GUILD_ID=0          # 0 = global sync (slower); set your server id for instant sync
VOICE_STT_PROVIDER=local         # local = faster-whisper (default, no cloud API)
VOICE_LOCAL_WHISPER_MODEL=base   # tiny/base/small/medium/large
# OPENAI_API_KEY=<key>           # Only needed for TTS or cloud STT fallback
# OLLAMA_AUTH_KEY=<key>          # Only needed for LLM channel-name slugs
```

### 3. Run

```bash
python -m opencode_discord_bot
```

The bot starts the Discord gateway, auto-spawns `opencode serve` as a child
process, and registers slash commands. Use `/oc <your prompt>` in Discord.

**Important:** enable the **Message Content** privileged intent in the
Discord Developer Portal (Bot -> Privileged Gateway Intents) — without it,
plain-text follow-ups silently break.

## Configuration

All settings are env vars (uppercase of the field name) or entries in a
`.env` file. See `.env.example` for the full list.

### Required

| Variable | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `OPENCODE_SERVER_PASSWORD` | Password for `opencode serve` basic auth |

### Optional

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_GUILD_ID` | `0` | Guild id for slash-command sync (0 = global, slower) |
| `DISCORD_BOT_ALLOWED_CHANNEL_IDS` | `[]` | Restrict slash commands to these channels (JSON list) |
| `DISCORD_BOT_SESSION_CATEGORY_ID` | `0` | Discord category for session channels (0 = no category) |
| `OPENCODE_SERVER_URL` | `http://127.0.0.1:4096` | opencode server URL |
| `OPENCODE_SERVE_ENABLED` | `true` | Auto-spawn `opencode serve` on login |
| `OPENCODE_SERVE_PORT` | `4096` | opencode server port |
| `VOICE_STT_PROVIDER` | `local` | `local` (faster-whisper) / `openai` (cloud) / `auto` |
| `VOICE_LOCAL_WHISPER_MODEL` | `medium` | faster-whisper model size |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type |
| `VOICE_STT_HOTWORDS` | (empty) | Space-separated domain words boosted by faster-whisper's decoder (local+auto only) |
| `VOICE_STT_PROMPT` | (empty) | Context sentence for Whisper's `initial_prompt` (local) / `prompt=` (cloud) |
| `VOICE_STT_REPLACEMENTS` | (empty) | JSON map of mishearings → corrections applied to the final transcript (e.g. `{"Conulec":"comulytic"}`) |
| `VOICE_TTS_ENABLED` | `true` | Speak responses in voice channels |
| `OPENAI_API_KEY` | (empty) | Needed for TTS + cloud STT (skip if TTS off + local STT) |
| `OLLAMA_AUTH_KEY` | (empty) | LLM channel-name slugs (skip = regex fallback) |
| `SLUG_MODEL` | `gpt-oss:20b-cloud` | Model for slug generation |

## Commands

| Command | Description |
|---|---|
| `/oc <prompt>` | Run a prompt as a new opencode session in a new channel |
| `/oc_plan <prompt>` | Run a prompt in plan mode (outlines without executing) |
| `/oc_new` | Unbind the current channel (ignore further plain-text) |
| `/oc_session` | Show the bound session id for the current channel |
| `/oc_abort` | Abort the opencode session bound to the current channel |
| `/oc_sessions` | List all active opencode sessions |
| `/oc_voice <mode>` | Record a spoken prompt in your voice channel (mode: change/note) |
| `/oc_voice_stop` | Stop an in-progress /oc_voice recording |
| `/oc_talk` (with attachment) | Transcribe an audio/video attachment and run it as a prompt |
| `/oc_cleanup` | Delete all bot-created session channels in the session category (requires Manage Channels) |
| `/oc_setup` | One-time guild setup: creates the "OpenCode Sessions" category + `voice-recordings` + `bot-commands` channels, writes their IDs to `.env`, reloads config (requires Manage Channels) |
| `/oc_help` | Post an ephemeral summary of every command |

After `/oc` or `/oc_plan` creates a session channel, any plain-text message in
that channel is forwarded as a follow-up prompt to the bound opencode session.

## Session monitor (optional, read-only)

When opencode is running a long task on your **desktop**, the session
monitor posts a Discord embed per event so you can step away and still know
when to come back — to approve a permission, answer a question, or pick up
a finished session. **Read-only**: the monitor never approves, answers, or
aborts anything; it only posts embeds.

| Event | Embed |
|---|---|
| A desktop session is waiting on a **permission** | 🔐 red — permission name, matched patterns, metadata |
| A desktop session is waiting on a **question** | ❓ orange — question header, options, multi-select/custom tags |
| A watched desktop session **completed** | ✅ green — ~300-char snippet of the final response |

Sessions already bound to a Discord channel (`/oc`, `/oc_plan`, bridge
channels) are excluded — they already notify in their own channels.
Sessions idle when the bot starts are never notified (no restart spam).

| Variable | Default | Description |
|---|---|---|
| `MONITOR_ENABLED` | `false` | Master switch (the monitor never starts unless this is `true`) |
| `MONITOR_CHANNEL_ID` | `1544715093491847249` | The channel the embeds go to |
| `MONITOR_USER_ID` | `0` | Your Discord user id — @mentioned in each embed's content so your phone buzzes (0 = no mention) |
| `MONITOR_POLL_INTERVAL_SECONDS` | `10` | Poll cadence for the status/question/permission GETs |

On Fly, the monitor watches your desktop `opencode serve` over the same
Tailscale tunnel the rest of the bot uses. Set
`MONITOR_ENABLED=true MONITOR_USER_ID=<your id>` as Fly secrets (or in
`/data/.env` — none of the monitor vars are sensitive).

## Dashboard (optional, token-gated)

The bot can serve an in-process ops dashboard for watching + controlling the
Comulytic bridge. It only starts when **both** `DASHBOARD_ENABLED=true` and a
non-empty `DASHBOARD_TOKEN` are set (an empty token means it never starts).
Every request must present the token via `Authorization: Bearer <t>` or
`?token=<t>`.

**Stats tab:** processed / skipped / failed counters, last poll time + status,
in-flight recording, seen-set size, per-recording history, bridge flags,
uptime, memory RSS, session bindings (both router files), and the last ~500
log lines — no more `flyctl logs` for recent history.

**Controls tab** (all toggles reset on restart):

| Control | What it does |
|---|---|
| Skip transcription ON/OFF | While ON, every NEW recording from Comulytic is marked processed (seen) but never transcribed or routed to Bobby — the accidental-recording kill switch. |
| Pause / resume polling | Paused = poll cycles skipped entirely; nothing is marked seen, so the backlog processes on resume. (Pause HOLDS; skip DROPS.) |
| Abort current recording | Cancels the recording being transcribed right now (it's already marked seen — it won't reprocess). |
| Mark ALL current recordings as seen | Bulk-skips everything currently on Comulytic with one click. |
| Clear seen-set | Destructive: everything on Comulytic reprocesses on the next cycle. |
| Live config | Change poll interval, page size, and the max-duration cap on the running process (reverts on restart). |

### On Fly.io

The app's `[[services]]` block maps `https://opencode-discord-bot.fly.dev` to
the dashboard port. After deploying the dashboard-enabled image:

```bash
flyctl secrets set DASHBOARD_ENABLED=true DASHBOARD_TOKEN=<random long string>
flyctl deploy
```

Then open `https://opencode-discord-bot.fly.dev/?token=<token>`.

### Locally

```bash
DASHBOARD_ENABLED=true DASHBOARD_TOKEN=devtoken python -m opencode_discord_bot
# open http://localhost:8080/?token=devtoken
```

## Voice Setup

The default `voice_stt_provider=local` uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) for
100% local transcription — no cloud API, no per-request cost,
privacy-preserving. The model downloads on first use and is cached.

### faster-whisper models

| Model | Size | Accuracy |
|---|---|---|
| `tiny` | ~75MB | Lowest |
| `base` | ~140MB | Good (CPU-feasible) |
| `small` | ~480MB | Better |
| `medium` | ~1.5GB | High (default) |
| `large` | ~3GB | Highest |

### TTS (optional)

TTS (bot speaks responses in the voice channel) uses OpenAI's cloud TTS API.
Set `VOICE_TTS_ENABLED=false` to disable it, or set `OPENAI_API_KEY` to use it.

### /oc_voice recording (broken)

Pycord 2.8.1's voice reception is broken by Discord's DAVE (End-to-End
Encryption) protocol on modern voice channels — `/oc_voice` recording does
not currently work, and a replacement voice-capture solution is being sought.
The text commands, TTS, STT, and session routing are unaffected. See
`SETUP_GUIDE.md` "/oc_voice recording (broken)" for context.

## Deploy in a container

For long-running hosting (so the bot stays connected to the Discord
gateway without keeping a local terminal open), run it in a container.
This repo ships the files you need, designed for any container host that
supports a persistent volume + an entrypoint script:

| File | Purpose |
|---|---|
| `Dockerfile` | Two-stage build: installs the bot + its Python deps into a venv, then a slim runtime image with `ffmpeg` (the voice pipeline imports at module top, so ffmpeg must be present) |
| `fly.toml` | Example app config (machine size, persistent volume mount, build/dockerfile) — adapt to your host |
| `fly.env.example` | Template for the non-secret `.env` that lives on the persistent volume |
| `start.sh` | Container entrypoint: joins a Tailscale tailnet (if `TAILSCALE_AUTHKEY` is set), seeds `/data/.env` from the template on first boot, then `exec`s the bot from `/data` |
| `.dockerignore` | Keeps the Docker build context lean (excludes `.venv`, `Speakers/`, `tests/`, local state) |

### Key design choices (portable across hosts)

- **Outbound-only.** The bot has no inbound ports — it connects out to
  the Discord gateway (WebSocket), your `opencode serve` instance, the
  Comulytic API, and (optionally) OpenAI. No `EXPOSE` / no public ingress
  is needed. Most container hosts work out of the box.
- **No in-container `opencode serve` (by default).** The Dockerfile does
  NOT install the `opencode` binary. Instead, set
  `OPENCODE_SERVE_ENABLED=false` + `OPENCODE_SERVER_URL=http://<your-opencode-serve>:4097`
  so the bot talks to an `opencode serve` running elsewhere (your desktop,
  a separate machine, another container). To run a self-contained server
  in the same container instead, install `opencode` in the Dockerfile
  (`RUN npm install -g opencode-ai`) and set `OPENCODE_SERVE_ENABLED=true`.
- **Persistent volume at `/data`.** The bot writes `.env`,
  `.opencode-discord-bot-sessions.json`, `.opencode-discord-bridge-sessions.json`,
  `.comulytic-seen.json`, and the faster-whisper model cache here.
  Without a persistent volume these are lost on every restart — follow-ups
  break, the bridge re-processes every recording, and the Whisper model
  re-downloads every boot. Mount a volume at `/data` (and set
  `HF_HOME=/data/hf-cache` to cache the Whisper model on it).
- **Secrets vs `.env`.** Put sensitive values (bot token, server
  password, API keys) in your host's secret manager (env vars that
  override `.env` in pydantic-settings). Put non-sensitive runtime config
  + the guild-specific IDs in `/data/.env` so `/oc_setup` can write them
  at runtime. The guild IDs (`DISCORD_BOT_GUILD_ID`,
  `DISCORD_BOT_SESSION_CATEGORY_ID`, `DISCORD_BOT_ALLOWED_CHANNEL_IDS`,
  `VOICE_MESSAGE_TRIGGER_CHANNEL_ID`) MUST be in the `.env` file, NOT
  secrets — env vars shadow `.env` and `/oc_setup`'s atomic write would
  be silently ignored.
- **No auto-stop.** The bot must stay connected to the Discord gateway to
  receive slash commands + plain-text follow-ups in real time. Disable any
  "scale to zero" / auto-stop feature your host provides.
- **Machine size.** ~2GB RAM is comfortable (faster-whisper `base` model +
  the bot + Pycord + the Comulytic bridge + httpx). 1GB is tight when the
  bridge + a Whisper transcription run concurrently. The bot is I/O-bound
  (gateway + HTTP polling), not CPU-bound except during transcription.
- **What's NOT in the image:** the `opencode` binary (remote serve), the
  `speakers` extra (pyannote.audio + torch, ~1-2GB — speaker ID degrades
  to anonymous STT; set `SPEAKER_ID_ENABLED=false`), and TTS
  (`VOICE_TTS_ENABLED=false` — TTS is for `/oc_voice` playback, which is
  DAVE-broken). ffmpeg IS installed.

### First-run flow (any host)

1. Set secrets (token, server password, server URL,
   `OPENCODE_SERVE_ENABLED=false`, any API keys). If using Tailscale to
   reach a remote `opencode serve`, set `TAILSCALE_AUTHKEY`.
2. Deploy the image + mount a persistent volume at `/data`.
3. On first boot `start.sh` seeds `/data/.env` from `fly.env.example`.
   Re-seed manually if needed: copy the template to `/data/.env`.
4. Sync slash commands (the bot does NOT auto-sync on startup):
   `python -m opencode_discord_bot.sync_commands --guild <guild id>`
   (run inside the container, from `/data`).
5. In Discord, invoke `/oc_setup` (requires the Manage Channels
   permission) — it creates the "OpenCode Sessions" category +
   `voice-recordings` + `bot-commands` channels and writes their IDs to
   `/data/.env`. Then stop, re-sync commands, and restart so every
   command is registered.

## Install as an opencode skill

Add the skill URL to your `opencode.json` (or `opencode.jsonc`) `skills`
array:

```json
{
  "skills": [
    "https://raw.githubusercontent.com/jakestrouse00/opencode-discord-bot/main/"
  ]
}
```

Opencode fetches `<url>/index.json`, finds the `discord-bot` skill,
downloads `<url>/discord-bot/SKILL.md`, and makes it available to the LLM
via the `skill` tool. The skill teaches the LLM how to install, configure, and
run the bot.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot exits with "discord_bot_token is empty" | Set `DISCORD_BOT_TOKEN` in `.env` or env |
| Slash commands don't appear | Set `DISCORD_BOT_GUILD_ID` to your server id (0 = global, takes ~1hr) |
| Plain-text follow-ups ignored | Enable Message Content intent in Discord Developer Portal |
| `/oc_voice` produces no audio | Pycord 2.8.1 voice reception broken by DAVE — recording does not currently work (see "/oc_voice recording (broken)" above) |
| `/oc_voice` / `/oc_talk` fails with ffmpeg error | Install ffmpeg on PATH |
| "opencode serve: binary not found" | Install opencode (`npm install -g opencode-ai`) |
| "server did not become healthy" | Port 4096 in use, or `OPENCODE_SERVER_PASSWORD` wrong |
| First `/oc_voice` is slow | faster-whisper model downloading (happens once, then cached) |

### Useful commands

```bash
# Check token is set (without revealing it)
python -c "from opencode_discord_bot.config import config; print('set' if config.discord_bot_token else 'EMPTY')"

# Force sync slash commands (no gateway, no serve)
python -m opencode_discord_bot.sync_commands --guild <your guild id>

# Check opencode server is reachable
curl -u opencode:<password> http://127.0.0.1:4096/global/health

# Verify faster-whisper loads
python -c "from faster_whisper import WhisperModel; m=WhisperModel('tiny', device='cpu', compute_type='int8'); print('ok')"
```