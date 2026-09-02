# AGENTS.md

Repo-specific guidance for AI agents working with the opencode Discord
control bot.

## Run

- **Install:** `pip install git+https://github.com/jakestrouse00/opencode-discord-bot.git`
  (or `pip install -e .` from a local clone). Installs the
  `opencode_discord_bot` Python package + a `opencode-discord-bot` console
  script.
- **System deps (NOT pip-installable):** Python 3.13+, `ffmpeg` on `PATH`
  (for `/oc_voice` / `/oc_talk` audio extraction + TTS playback), `opencode`
  on `PATH` (the bot auto-spawns `opencode serve` on login via
  `OpencodeBot.on_connect`).
- **Configure:** set `DISCORD_BOT_TOKEN` (required) +
  `OPENCODE_SERVER_PASSWORD` (required) + optional `OPENAI_API_KEY` (TTS +
  cloud STT fallback) + optional `OLLAMA_AUTH_KEY` (LLM channel-name slugs) +
  optional `OPENCODE_DEFAULT_MODEL` / `OPENCODE_ASSISTANT_MODEL` (override
  the opencode model for `/oc`/follow-ups vs `/oc_plan`/`/oc_voice`/`/oc_talk`/
  bridge) via env vars or a `.env` file in the run directory. See `.env.example`
  for the full list.
- **Install the `oc-assistant (Bobby)` agent:** the bot's `/oc_plan`, `/oc_voice`,
  `/oc_talk`, voice-message trigger, and Comulytic-bridge paths route to
  opencode's `oc-assistant (Bobby)` agent, which is **not** built into opencode — it
  lives in the target project's `.opencode/agent/oc-assistant.md`. This package
  ships a generic, self-contained copy. Run
  `python -m opencode_discord_bot.install_agent` (from the target project's
  root, or `--dest <project root>`) to install it. See `SETUP_GUIDE.md`
  "Install the oc-assistant (Bobby) agent" for details.
- **Do NOT run the bot from agent context.** `python -m opencode_discord_bot`
  (or `opencode-discord-bot`) starts the Discord gateway AND auto-spawns
  `opencode serve` as a child process (unless `OPENCODE_SERVE_ENABLED=false`).
  Both outlive the agent's bash-tool command and keep running with no TUI to
  stop them, which blocks the next session from binding the port and leaves
  zombie processes. The bot is meant to be launched manually by a human in
  its own terminal, not by an agent. For verification, use the import check
  in the "No tests..." bullet below — never launch the gateway.
- **Slash commands are NOT auto-synced on startup** — `auto_sync_commands=False`
  is set in `OpencodeBot.__init__` to prevent Pycord's `on_connect` from
  pushing a global copy of every command on every login (which, combined
  with the guild-scoped commands pushed by `sync_commands.py`, produced
  duplicate entries in the Discord UI). Sync commands explicitly via
  `python -m opencode_discord_bot.sync_commands --guild <id>` after any
  change to the slash-command surface — this is a one-shot sync a human
  runs manually, NOT something an agent should invoke.
- **Privileged intent:** the **Message Content** gateway intent MUST be
  enabled in the Discord Developer Portal (Bot -> Privileged Gateway Intents)
  for plain-text follow-ups to work. The bot sets `intents.message_content =
  True` in `OpencodeBot.__init__`; the portal toggle is separate. Without it,
  `message.content` is always empty and follow-ups silently break.
- **Tests:** pytest + pytest-asyncio (in the shared root `.venv`). Run from
  `opencode-discord-bot/`:
    - `pytest` — full suite
    - `pytest tests/unit` — unit tests only (fast; pyannote-guarded ones skip
      when `HF_TOKEN` is unset)
    - `pytest tests/chain` — chain tests (real STT + ffmpeg on the committed
      `Speakers/Jake/clip*.mp3` fixtures; pyannote variants skip without
      `HF_TOKEN`)
    - `pytest -k comulytic` — only Comulytic-chain tests
  No network, no gateway, no real opencode/Discord/Comulytic calls — all
  network boundaries are scriptable fakes in `tests/fakes.py`. STT + ffmpeg
  run real on the committed sample clips; speaker-ID tests auto-skip when
  `HF_TOKEN` is unset or `pyannote.audio` isn't importable (the ~1-2GB
  first-run HuggingFace download needs the token + license acceptance).
- **No linter, no type-checker, no build step** are configured in this repo.
  The only other automated check is the Python import sanity check:
  `python -c "from opencode_discord_bot.commands import OpencodeBot; print('ok')"`.

## Deployment (Fly.io)

This is the production deployment the project is developed against. The
bot runs on **Fly.io** as a primarily outbound-only container — it connects
out to the Discord gateway, Tailscale's coordination servers, the user's
desktop `opencode serve` over a private Tailscale tunnel, the Comulytic
API, and (optionally) OpenAI. The ONE inbound surface is the token-gated
ops dashboard (`[[services]]` → internal port 8080), which 401s every
request without `DASHBOARD_TOKEN`. The deployment files live at the repo
root: `Dockerfile`, `fly.toml`, `fly.env.example`, `start.sh`,
`.dockerignore`.

- **No in-container `opencode serve`.** The bot's local default
  (`OPENCODE_SERVE_ENABLED=true`) auto-spawns `opencode serve` as a child
  process — NOT used on Fly. The deploy sets `OPENCODE_SERVE_ENABLED=false`
  + `OPENCODE_SERVER_URL=http://<desktop-tailscale-ip>:4097` (a Fly secret)
  so the bot talks to the user's **desktop** `opencode serve` over a
  Tailscale tunnel. The `opencode` binary is intentionally NOT installed in
  the image. To run a self-contained server in-container instead, install
  opencode in the Dockerfile and flip `OPENCODE_SERVE_ENABLED=true`.
- **Tailscale in the image.** The Dockerfile copies `tailscaled` +
  `tailscale` from the official Tailscale image; `start.sh` starts the
  daemon, joins the tailnet (`tailscale up --auth-key=$TAILSCALE_AUTHKEY
  --hostname=discord-bot --accept-routes`), then launches the bot. The
  Tailscale auth key should be **reusable + ephemeral + pre-authorized**
  (ephemeral = the node auto-removes from the tailnet when the machine
  stops, keeping the tailnet clean). Daemon state persists at
  `/data/tailscaled.state` so the node identity is stable across restarts.
  Tailscale is the reason the desktop `opencode serve` (not exposed to the
  public internet) is reachable from the container.
- **Persistent volume at `/data`** (Fly `[[mounts]] source="bot_data"
  destination="/data"`). Holds: `.env` (written by `/oc_setup`),
  `.opencode-discord-bot-sessions.json`, `.opencode-discord-bridge-sessions.json`,
  `.comulytic-seen.json`, the faster-whisper model cache
  (`HF_HOME=/data/hf-cache`), and `tailscaled.state`. `WORKDIR /data` +
  `start.sh` `cd /data` so pydantic-settings finds `.env` (cwd-relative) and
  the router/seen-set files land on the volume. Without the volume, all of
  this is lost on every restart — follow-ups break, the bridge re-processes
  every Comulytic recording, and the Whisper model re-downloads.
- **Secrets vs `.env` split (load-bearing).** Sensitive values are Fly
  secrets (`flyctl secrets set`) — env vars that override `.env` in
  pydantic-settings: `DISCORD_BOT_TOKEN`, `OPENCODE_SERVER_PASSWORD`,
  `OPENCODE_SERVER_URL`, `OPENCODE_SERVE_ENABLED`, `TAILSCALE_AUTHKEY`,
  `COMULYTIC_JWT`/`COMULYTIC_REFRESH_TOKEN`, `OPENAI_API_KEY`,
  `OLLAMA_AUTH_KEY`, and the dashboard pair `DASHBOARD_ENABLED=true` +
  `DASHBOARD_TOKEN=<random>` (the dashboard only starts when BOTH are set;
  `DASHBOARD_TOKEN` must be a long random string — every dashboard request
  must present it). Non-sensitive runtime config + the guild-specific IDs
  live in `/data/.env` (seeded from `fly.env.example` on first boot via
  `start.sh`). The guild IDs (`DISCORD_BOT_GUILD_ID`,
  `DISCORD_BOT_SESSION_CATEGORY_ID`, `DISCORD_BOT_ALLOWED_CHANNEL_IDS`,
  `VOICE_MESSAGE_TRIGGER_CHANNEL_ID`) MUST be in `.env`, NOT Fly secrets —
  env vars shadow `.env` and `/oc_setup`'s atomic write would be silently
  ignored, so the IDs would never persist. `/oc_setup` writes them to
  `/data/.env` at runtime.
- **Machine:** 2GB RAM, `shared-cpu-1x`, 1 CPU (see `fly.toml [[vm]]`).
  faster-whisper `small` (~244MB model, ~1-1.2GB RSS at load) + the bot +
  Pycord + the Comulytic bridge + httpx clients. 2GB is adequate but
  tighter than `base` was — concurrent bridge + transcription is the risk
  window (bump to 4GB if OOMs appear in `flyctl logs`). The bot is
  I/O-bound (Discord gateway + HTTP polling), not CPU-bound except during
  transcription. **No auto-stop/autostart** — the bot must stay connected to
  the Discord gateway to receive slash commands + plain-text follow-ups in
  real time, so Fly's auto-stop is NOT used.
- **What's NOT in the image:** the `opencode` binary (remote serve over
  Tailscale instead), the `speakers` extra (pyannote.audio + torch,
  ~1-2GB — speaker ID degrades to anonymous STT, so
  `SPEAKER_ID_ENABLED=false`), and TTS (`VOICE_TTS_ENABLED=false` — TTS is
  for `/oc_voice` playback, which is DAVE-broken). ffmpeg IS installed
  (the bot imports the voice pipeline unconditionally at module top, so
  ffmpeg must be present to avoid import-time failures).
- **Dockerfile `sed` quirk:** the builder stage runs
  `sed -i '/^force-include = /d' /src/pyproject.toml` because newer
  hatchling already includes non-`.py` files from `packages`, so the
  `force-include` line in `pyproject.toml` duplicates
  `agent/oc-assistant.md` and the wheel build fails with "A second file is
  being added to the wheel archive at the same path". The `.md` agent file
  is still shipped (it's under `src/opencode_discord_bot/agent/` which is in
  `packages`). Do NOT remove the `sed` without also removing the
  `force-include` line from `pyproject.toml`.
- **Deploy + first-run flow:**
  1. `flyctl secrets set DISCORD_BOT_TOKEN=... OPENCODE_SERVER_PASSWORD=...
     OPENCODE_SERVER_URL=http://<desktop-tailscale-ip>:4097
     OPENCODE_SERVE_ENABLED=false TAILSCALE_AUTHKEY=tskey-...` (+ optional
     `COMULYTIC_*`, `OPENAI_API_KEY`, `OLLAMA_AUTH_KEY`, voice settings,
     and — since the dashboard landed — `DASHBOARD_ENABLED=true
     DASHBOARD_TOKEN=<random>`).
  2. `flyctl ips allocate-v4 --shared` + `flyctl ips allocate-v6` —
     one-time. Provisions the public ingress IPs that back
     `https://<app>.fly.dev` and the dashboard's `[[services]]` block.
     Fly does NOT auto-allocate these on deploy: the app was originally
     outbound-only (zero IPs), and a `[[services]]` block alone leaves the
     hostname DNS-less with a "can't connect" symptom. Verify with
     `flyctl ips list` (expect a shared v4 + a v6).
  3. `flyctl deploy` (builds the Dockerfile, provisions the `bot_data`
     volume on first deploy).
  4. First boot: `start.sh` seeds `/data/.env` from `fly.env.example`.
  5. `flyctl ssh console -C "cp /app/fly.env.example /data/.env"` if a
     re-seed is ever needed (the template is copied into the image at
     `/app/fly.env.example`).
  6. Sync slash commands:
     `flyctl ssh console -C "cd /data && /venv/bin/python -m opencode_discord_bot.sync_commands --guild <id>"`
     (the bot does NOT auto-sync on startup).
  7. Start/restart the machine so the bot runs. In Discord, invoke
     `/oc_setup` (requires Manage Channels) — it creates the "OpenCode
     Sessions" category + `voice-recordings` + `bot-commands` channels and
     writes their IDs + the guild id to `/data/.env`.
  8. Stop, re-sync commands (so every command is registered now that
     `/oc_setup` has written the guild config), restart.
- **`OPENCODE_SERVE_CWD` on Fly:** not set — the bot does NOT spawn
  `opencode serve` on Fly (`OPENCODE_SERVE_ENABLED=false`), so the serve
  CWD is irrelevant to the container. The desktop `opencode serve` the bot
  talks to runs in the user's project directory on their desktop (where
  `.opencode/` lives); the bundled `oc-assistant (Bobby)` agent must be installed
  there (`python -m opencode_discord_bot.install_agent --dest <desktop
  project root>`), not in the container.

## Architecture

- **Package layout:** `src/opencode_discord_bot/` (pip-installable, importable
  as `opencode_discord_bot`). The `src/` layout means the package is only
  importable after `pip install -e .` (or adding `src/` to `PYTHONPATH` for
  development). Top-level files (`index.json`, `discord-bot/SKILL.md`,
  `AGENTS.md`, `README.md`, `pyproject.toml`, `.env.example`) sit at the repo
  root, NOT inside the package. Notable modules beyond the ones called out
  below: `singleton.py` (the `SingletonLock` single-instance guard — see
  its own bullet), `env_writer.py` (the idempotent atomic `.env` updater
  used by `/oc_setup`), `bot_start.py` (a thin script wrapper over
  `__main__.main` for launching without the `-m` flag — not in
  `[project.scripts]`, kept for convenience).
- **`OpencodeBot(discord.Bot)`** — the main bot class
  (`opencode_discord_bot/commands.py`). Owns the slash-command surface
  (`/oc`, `/oc_plan`, `/oc_new`, `/oc_session`, `/oc_abort`, `/oc_sessions`,
  `/oc_voice`, `/oc_voice_stop`, `/oc_talk`, `/oc_cleanup`, `/oc_setup`,
  `/oc_help`) + the plain-text follow-up path (`on_message`) + the
  edit-to-revert path (`on_message_edit`). Each `/oc` or `/oc_plan` invocation
  creates a fresh opencode session AND a fresh Discord text channel under the
  configured category (`discord_bot_session_category_id`), then posts the
  response there. Subsequent plain-text messages in that session channel are
  forwarded to the bound opencode session as follow-up prompts; editing one's
  own follow-up message aborts the running session, reverts to the mapped
  opencode user message, and resends the edited text as a fresh prompt
  (mirrors the opencode GUI's stop-revert-edit-resend flow). The bot ignores
  messages in channels it did not create.
  `/oc_cleanup` is the one destructive maintenance command: it deletes every
  text channel under `discord_bot_session_category_id` and clears the matching
  `SessionRouter` bindings (via `router.reset`), leaving the category itself
  intact for reuse. It requires the Manage Channels permission (gated in the
  callback via `ctx.author.guild_permissions.manage_channels`) and is intended
  for cleaning up the server between test sessions. Discovery is strictly
  category-scoped (`category.text_channels`), so allowlisted command channels,
  voice channels, and user-created channels are never enumerated or deleted.
  `/oc_setup` is the one-time guild setup command (requires Manage Channels):
  it creates the "OpenCode Sessions" category plus two guild-root text channels
  (`voice-recordings` → `VOICE_MESSAGE_TRIGGER_CHANNEL_ID`, `bot-commands` →
  `DISCORD_BOT_ALLOWED_CHANNEL_IDS`), writes their IDs + the guild id to `.env`
  atomically via `env_writer.update_env_file`, reloads the `config` singleton
  in place via `reload_config()`, and refuses to run twice (gates on whether
  any of the three guild-specific output fields is already non-default). See
  SETUP_GUIDE.md "`/oc_setup` — one-time guild setup" for the full flow.
  `/oc_help` posts an ephemeral summary of every command.
- **`OpencodeClient`** (`opencode_client.py`) — async `httpx` wrapper over
  the opencode server REST API (sessions, messages, questions, permissions,
  events). No official Python SDK exists (the SDK is JS/TS-only), so this is
  a thin typed surface over the documented REST endpoints. Auth: basic auth
  via `OPENCODE_SERVER_PASSWORD` env var. **Model overrides:**
  `send_prompt_async` / `send_message` resolve the model id from
  `config.opencode_default_model` (for `agent=None` — `/oc` + plain-text
  follow-ups) or `config.OPENCODE_ASSISTANT_MODEL` (for `agent="oc-assistant"`
  — `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message trigger, Comulytic
  bridge) via `_resolve_model`, and include it in the POST body only when
  non-empty. Both empty (the default) = each opencode agent's own frontmatter
  `model:` field wins (the historical behavior). No call site passes `model`
  explicitly — the override flows through transparently.
- **Bundled `oc-assistant` agent (Bobby)** (`opencode_discord_bot/agent/oc-assistant.md`)
  — a fully generic, self-contained assistant subagent ("Bobby") shipped
  inside the package. The bot routes `oc-assistant` prompts to opencode's
  `oc-assistant` agent, which the target project must have installed at
  `.opencode/agent/oc-assistant.md`. The bundled copy is project-agnostic
  (it reads the target project's own `AGENTS.md` if present, writes only
  under `.opencode/assistant/{plans,notes,thoughts}/`, and defaults to the
  **thought** type when no type signal is present). Bobby's artifacts live
  under `.opencode/assistant/` and are intentionally NOT visible to the
  root toolkit's `plan-dashboard` / `plan-triage` / `change-outline` (which
  read `.opencode/plans/`) — the user doesn't want other opencode sessions
  routing to this agent, so cross-visibility is dropped by design. Install
  it via `python -m opencode_discord_bot.install_agent` (see
  `install_agent.py`). Shipped in the wheel via hatchling `force-include`.
- **`OpencodeServe`** (`opencode_serve.py`) — lifecycle manager for the
  `opencode serve` subprocess. `OpencodeBot.on_connect` spawns it on login
  (guarded by `_serve_started` so reconnects don't re-spawn);
  `OpencodeBot.close` tears it down on shutdown. Binary discovery:
  `shutil.which("opencode")` first, then `npx -y -p opencode-ai opencode`
  (npm fallback). Readiness: polls `GET /global/health` until 200 or
  `startup_timeout` elapses. Teardown: Windows `taskkill /T /F` (tree-kill),
  POSIX `SIGTERM` → `SIGKILL` after grace. Idempotent. `_REPO_ROOT` defaults
  to `Path.cwd()` so `opencode serve`'s `process.cwd()` resolves to the
  user's current project directory (where their `.opencode/` lives) — NOT
  the package install dir. Override via `OPENCODE_SERVE_CWD` (absolute path)
  to decouple the server's project dir from the bot's launch dir — useful
  when the bot's `.env` lives in a subdirectory but the user's `.opencode/`
  (plans, agents, config) lives at the project root; without it, sessions
  resolve to the subdir and can't find plans/agents (the silent plan-loss
  bug). Windows reliability backstop: `_assign_kill_on_close_job` assigns
  the subprocess tree to a Windows Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` so the `opencode serve` tree is reaped
  if the Python parent is force-killed (TerminateProcess, segfault) before
  `try/finally`/`atexit` can run.
- **`SingletonLock`** (`singleton.py`) — OS-level file lock
  (`.opencode-discord-bot.lock`, gitignored) that prevents two bot processes
  from racing for the single Discord gateway session per bot token.
  `__main__.main` calls `SingletonLock.acquire_or_raise()` before importing
  `OpencodeBot` / starting the gateway; a second instance exits(1) with a
  clear message instead of causing "The application did not respond" errors
  from a dual-launch gateway race. The lock is auto-released by the OS on
  process exit (even on crash), so a crashed previous instance never leaves
  a stale lock. Override the lock path via `OC_SINGLETON_LOCK` (absolute
  path; empty = `.opencode-discord-bot.lock` in the launch cwd).
- **`SessionRouter`** (`session_router.py`) — maps Discord channel id ->
  opencode session id, persisted to `.opencode-discord-bot-sessions.json`
  (gitignored). One persistent opencode session per Discord channel. Loaded
  on construction, saved after each new binding. **Bridge-channel resume:**
  `OpencodeBot` consults BOTH this router AND the Comulytic bridge's router
  (`.opencode-discord-bridge-sessions.json`, the file the bridge writes when
  it creates a channel for a routed recording). The unified lookup
  `OpencodeBot._resolve_sid(channel_id)` returns the main bot's binding if
  present, else the bridge's. This powers `on_message` plain-text follow-ups
  (a user returning to a bridge-created channel hours later resumes the
  session via the existing `_run_followup` → `_drive_session` path, which
  spawns the main bot's button-based `poll_pending_requests`), plus
  `on_message_edit`, `_channel_ok`, and the `/oc_session` / `/oc_abort` slash
  commands. `/oc_new` and `/oc_cleanup` `reset` the binding in BOTH routers
  (`reset` is a no-op when the channel isn't bound). The bridge router handle
  is constructed lazily on first `_bridged_sid` call
  (`self.bridge_router` is `None` until then), so deployments without the
  bridge never open the file. The main bot only READS the bridge file (and
  `reset`s entries); the bridge OWNS writes to it, so the two processes never
  clobber each other. The filename is mirrored as a string constant in
  `commands.py:_BRIDGE_SESSIONS_FILE` (NOT imported from `bridge.py` to
  avoid pulling the bridge's full import surface into the main bot's module
  top) — keep the two constants in sync.
- **`BotConfig`** (`config.py`) — `pydantic-settings` `BaseSettings` with
  ONLY bot-relevant fields (Discord, opencode-serve, voice, faster-whisper,
  OpenAI, slug LLM). All defaults are empty/safe (NO committed secrets). The
  module-level `config = BotConfig()` singleton is shared across all modules.
  CLI overrides in `__main__.py` mutate the singleton in place.
- **Voice pipeline** (`voice.py`): `/oc_voice` joins a voice channel, records
  audio via Pycord's `start_recording` + a custom `SilenceDetectSink`, chunks
  the recording for near-real-time stop-phrase detection, transcribes each
  chunk via `transcribe_audio()` (dispatches on `voice_stt_provider`:
  `"local"` = faster-whisper CTranslate2, `"openai"` = cloud Whisper API,
  `"auto"` = local first, cloud fallback), and optionally synthesizes the
  response via OpenAI TTS (`synthesize_speech()`) for playback in the channel.
  `/oc_talk` extracts audio from an attachment via `extract_audio_to_wav()`
  (ffmpeg subprocess) then transcribes it. Three stop triggers: "Stop
  Conversation" phrase, `/oc_voice_stop`, or `voice_silence_timeout_seconds`
  of continuous silence.
- **Voice-message intake** (`on_message` in `commands.py`): a regular
  `discord.Message` with an `audio/ogg` / `application/ogg` attachment and
  usually empty `content` (a Discord "voice message" — press-hold mic in the
  mobile composer) is detected by `OpencodeBot._voice_attachment`, normalized
  via the existing `extract_audio_to_wav` + `transcribe_audio` pipeline
  (same as `/oc_talk`), and routed by `on_message`'s four-branch dispatch:
  (a) a voice message in a session channel → follow-up prompt via
  `_run_voice_followup`; (b) text in a session channel → existing text
  follow-up path (unchanged); (c) a voice message in the trigger channel
  (`voice_message_trigger_channel_id`, default `#new-plans` / channel id
  `1533242090862149842`) → new oc-assistant (Bobby) session via
  `_run_talk_from_message`; (d) else ignored. Gated by `voice_message_enabled`
  (config, default True). No `plan_type` directive is sent for voice-message
  new sessions (no slash-option UI) — the `oc-assistant (Bobby)` agent classifies from
  the transcript. No new deps; reuses the `/oc_talk` pipeline verbatim. Does
  NOT touch `/oc_voice` or the DAVE-broken sinks path.
- **Speaker identification** (`speakers.py`): layers per-speaker labels on
  top of the anonymous STT path so multi-speaker recordings (meetings, not
  just solo notes) produce a diarized, speaker-labeled transcript that
  oc-assistant (Bobby) can turn into coherent meeting notes. Three public functions:
  `load_speakers()` (reads reference embeddings from `Speakers/<name>/`
  subfolders), `identify_speakers()` (diarize + cosine-match each turn
  against reference embeddings + label unknowns `Speaker N`; unwraps the
  pyannote 4.x `DiarizeOutput` dataclass and reuses the precomputed
  `speaker_embeddings` rather than re-embedding per turn), and
  `transcribe_with_speakers()` (the async orchestrator that callers use —
  falls back to `voice.transcribe_audio` when speaker ID is disabled,
  pyannote isn't installed, or `Speakers/` is empty). Engine: pyannote.audio
  (diarization + 256-dim voice embeddings via
  `wespeaker-voxceleb-resnet34-LM`), an **optional** dep via the
  `speakers` extra: `pip install 'opencode-discord-bot[speakers]'` (pulls
  the heavy torch stack, ~1-2GB). The bot starts fine WITHOUT it — speaker
  ID degrades to the existing anonymous transcript. **Setup:** drop
  reference audio files (`.wav`/`.mp3`/`.m4a`/`.ogg`/`.flac`) into
  `Speakers/<name>/` (one subfolder per speaker; the folder name is the
  default speaker label) at the bot's repo root (i.e. next to `pyproject.toml`
  / `.env`, so it ships with the repo), or override the location via
  `SPEAKERS_DIR`. **Resolution of a relative `SPEAKERS_DIR`:** the bot
  tries the process cwd first, then falls back to its own repo root
  (derived from the package install location) — so launching from a parent
  dir (e.g. a multi-project workspace root) still finds `Speakers/` in the
  bot subdir. An absolute `SPEAKERS_DIR` is used as-is. A missing
  `Speakers/` logs a WARNING and yields `{}` from `load_speakers()` →
  anonymous STT. Adding/removing reference audio requires a bot restart
  (embeddings are cached per process, same convention as the whisper model
  singleton).   **HuggingFace:** pyannote.audio 3.x downloads pretrained
  models on first use — accept the model licenses + set `HF_TOKEN` before
  first run (runtime concern, not a code dep). Four gated models:
  `pyannote/speaker-diarization-3.1` (the pipeline),
  `pyannote/segmentation-3.0` (segmentation sub-model),
  `pyannote/wespeaker-voxceleb-resnet34-LM` (embedding sub-model), and
  `pyannote/speaker-diarization-community-1` (PLDA — a default param of
  the `SpeakerDiarization` pipeline class, not overridden by the 3.1
  config). The `hf_token` `BotConfig` field (env var `HF_TOKEN`) carries
  the token; `speakers.py` reads `config.hf_token` (not `os.environ`) so
  the `.env` entry "just works". **Scope:** the three
  meeting-capture entry points (`/oc_talk`, voice-message intake, Comulytic
  bridge) call `transcribe_with_speakers`; the live `/oc_voice` chunked
  path (`VoiceSession._transcribe_current_sink`, voice.py:580) stays on
  `transcribe_audio` (whole-file diarization doesn't fit the chunked model
  and `/oc_voice` is DAVE-broken anyway — see Known-broken).
- **Slug LLM** (`slug.py`): `generate_slug()` makes one `httpx` POST to an
  OpenAI-compatible `/chat/completions` endpoint (Ollama Cloud by default) to
  produce a Discord channel-name slug from the user's prompt. NEVER raises —
  on any error (incl. empty `ollama_auth_key`) it returns the regex-only
  fallback. So if `OLLAMA_AUTH_KEY` is unset, slugs silently degrade to regex
  — the bot still works.
- **`poll_until_idle`** (`events.py`): polls `GET /session/status` for a
  specific opencode session until it becomes idle (the prompt finished).
  Requires the session to be observed as "busy" at least once (or a grace
  period to elapse) before treating a missing/idle entry as terminal — guards
  against the fire-and-forget race where `prompt_async` returns 204 before the
  forked effect sets busy.
- **`poll_pending_requests`** (`questions.py`): polls `GET /question` and
  `GET /permission` for the session being driven, renders each pending
  request as Discord buttons / select menus, and POSTs the user's choice back
  to the matching REST endpoint so the deferred resolves and the agent turn
  resumes.
- **Comulytic bridge → Discord channel** (`bridge.py:route_to_assistant` +
  `discord_rest.py` + `bridge_questions.py` + `text_utils.py`): when
  `discord_bot_token` + `discord_bot_guild_id` are set, the bridge creates a
  Discord text channel for each routed Comulytic recording (mirrors
  `/oc_talk`), posts the transcript, fires an LLM slug rename, sends to
  `oc-assistant (Bobby)`, surfaces clarifying questions as plain-text prompts (polling
  `GET /channels/{id}/messages` for the user's reply via the REST-based
  `poll_pending_requests_rest`), and posts the final response. Uses **raw
  Discord REST via `DiscordRest`** (no Pycord, no gateway — safe to run
  alongside the main bot, which owns the gateway session). Binds channels in
  a **separate** persistence file `.opencode-discord-bridge-sessions.json`
  (NOT `.opencode-discord-bot-sessions.json`) so the bridge's channel bindings
  don't clobber the main bot's. **The main bot reads this file** (see the
  `SessionRouter` bullet above) so plain-text follow-ups in bridge-created
  channels resume the session after the bridge's initial turn ends; the
  bridge remains the sole WRITER. `text_utils.py` is the Pycord-free shared module for
  `_split_message`/`_extract_text`/`_final_assistant_text`/`_slugify_prompt`
  (imported by both `commands.py` and `bridge.py`). When Discord isn't
  configured, `route_to_assistant` falls back to log-only.
- **Comulytic bridge auto-spawn:** `OpencodeBot.on_connect` spawns the bridge
  as an in-process `asyncio.create_task` (calling `run_bridge()` directly, NOT
  `main()` — `main()` would create a second event loop + reconfigure root
  logging) when `config.comulytic_enabled` AND `config.comulytic_jwt` are both
  set. The bridge's own `OpencodeServe.start()` probe-healthy short-circuits
  (`_reused=True`, `stop()` no-op) so it reuses the bot's already-running
  `opencode serve` — no second subprocess, no port conflict. `OpencodeBot.close`
  cancels + drains the task (10s ceiling) so the bridge's `finally` (save
  seen-set, close clients, no-op reused serve) runs BEFORE the bot kills the
  real serve subprocess. A bridge crash is isolated by a `_bridge_guard`
  wrapper and cannot take the bot down. The `self._bridge_task is None` guard
  prevents a reconnect re-spawn. So: starting the bot (`python -m
  opencode_discord_bot`) with `COMULYTIC_ENABLED=true` + `COMULYTIC_JWT` set is
  now sufficient — no separate `comulytic-bridge` launch needed (the console
  script remains as a standalone manual override). Without those two env vars,
  no bridge task is created (silent no-op).
- **Comulytic bridge transcription is local-Whisper-only:** the bridge NEVER
  consults Comulytic's cloud ASR (`queryTranscribeResult` / `asrResultVO`).
  For every audio-delivered recording it downloads the audio (`download_audio_smart`,
  Path B proxy primary + Path A pre-signed fallback) and runs the SAME
  transcription pipeline `/oc_talk` uses — `voice.extract_audio_to_wav` (ffmpeg
  → mono 16kHz WAV) + `voice.transcribe_audio` (dispatches on
  `voice_stt_provider`: default `"local"` = in-process faster-whisper
  CTranslate2; `"openai"` = cloud Whisper API; `"auto"` = local first, cloud
  fallback). By default transcription is fully local and private, consistent
  with `/oc_talk`. `voice.py` imports Pycord at module top, so the bridge
  (in-process with the bot) has Pycord loaded once it processes its first
  recording — Pycord is a hard dep, so the standalone `comulytic-bridge`
  script is unaffected. The old `comulytic_audio_fallback` config flag (cloud
  ASR primary, local Whisper fallback) is GONE — local Whisper is the sole
  path, so there's nothing to fall back from.
- **Ops dashboard** (`dashboard.py` + `dashboard_state.py`): a token-gated
  Starlette + uvicorn HTTP dashboard served **in-process** (one event loop
  with the bot + bridge, started from `OpencodeBot.on_connect` via
  `start_dashboard()`, stopped in `close()` via `stop_dashboard()`). Only
  starts when `DASHBOARD_ENABLED=true` AND a non-empty `DASHBOARD_TOKEN`
  are both set — an empty token means the dashboard NEVER starts (local
  default + Fly deployments without the secrets are unchanged). Auth:
  every request (page + API) presents the token via
  `Authorization: Bearer <t>` or `?token=<t>` (constant-time compare);
  the HTML page keeps the query token and fetches APIs client-side.
  `dashboard_state.py` is the shared-state module (mirrors the
  `bridge_state.py` pattern — module-level state, race-free on the shared
  loop, no imports from the bridge/dashboard surface). **Stats tab:**
  bridge metrics (processed/skipped/failed counters, last poll time +
  status, in-flight note id, seen count, recent-recordings history), system
  health (uptime, RSS from /proc), session bindings (read-only render of
  BOTH router JSON files), and the last ~500 log lines (in-memory ring via
  `dashboard_state.RingLogHandler` on the root logger, uvicorn loggers
  excluded). **Controls tab** (all EPHEMERAL — restart resets every toggle,
  an explicit user decision so a forgotten kill switch can't drop
  recordings after a redeploy):
  - *Skip transcription*: while ON, `poll_once` marks new audio-delivered
    recordings as seen WITHOUT transcribing/routing (the
    accidental-recording kill switch — recordings are silently dropped).
  - *Pause/resume*: while paused, the poll loop skips `poll_once`
    entirely — nothing is marked seen, so the backlog processes on resume
    (deliberately different semantics from skip: pause HOLDS, skip DROPS).
  - *Seen-set management*: "mark all current as seen" (bulk-skip
    everything on Comulytic) and "clear seen-set" (destructive —
    everything reprocesses). Both are queued as a pending action in
    `dashboard_state` and applied by the bridge task at the top of the
    next poll cycle (`_apply_pending_actions`) — the bridge owns ALL
    writes to `.comulytic-seen.json`; the dashboard never mutates the set
    directly.
  - *Abort in-flight*: cancels the per-recording task registered via
    `dashboard_state.set_in_flight` (the recording is wrapped in its own
    `asyncio.create_task` inside `poll_once`'s loop, so abort kills ONLY
    that recording, never the bridge task). The recording is already in
    `seen`, so it won't reprocess.
  - *Live config tweaks*: `POST /api/config` mutates the `config` singleton
    in place (poll interval, page size, max-duration h/m/s); the poll
    loop reads these per-cycle so changes apply immediately. A restart
    reverts to `.env`/Fly-secret values.
  Deps: `starlette` + `uvicorn` (in `pyproject.toml`); tests use
  `starlette.testclient.TestClient` (httpx transport — no server, no
  network). The dashboard is NOT started from the standalone
  `comulytic-bridge` console script (bot-process lifecycle only).

## Conventions

- **Pycord, NOT discord.py.** This bot uses `py-cord[voice]` 2.8.1 (Pycord, the
  `discord.py` fork) because Pycord provides the voice recording/sinks API
  (`VoiceClient.start_recording` / `discord.sinks.Sink`) that `discord.py`
  removed in 2.4.0 and never restored. Do not install `discord.py` alongside
  Pycord — they conflict. The command model is decorator-based
  (`@bot.slash_command` + `discord.Option` as a default value), NOT the
  `app_commands.CommandTree` model.
- **`discord.Option(...)` as a default value, NOT an annotation.** PEP 563
  (`from __future__ import annotations`) makes annotations strings, causing
  `issubclass() arg 1 must be a class` at invoke time. Use
  `param: str = discord.Option(str, "desc")`, not
  `param: discord.Option(str, "desc")`.
- **`Modal.callback`, NOT `on_submit`.** Pycord's `BaseModal` dispatches
  `callback`, not `on_submit`; `on_submit` silently never fires.
- **`discord.InputTextStyle`, NOT `discord.TextStyle`.** The latter doesn't
  exist in Pycord.
- **`__sink_listeners__: list = []` on `SilenceDetectSink`.** Pycord 2.8.1's
  `SinkEventRouter._register_listeners` accesses `sink.__sink_listeners__`
  unconditionally, but the sinks module never defines it on the base `Sink`
  class — a library bug. The empty list makes the router's loop iterate zero
  times instead of raising `AttributeError`. Same class of bug as
  `walk_children` returning `[]`.
- **No committed secrets.** All `BotConfig` defaults are empty strings / safe
  defaults. The user sets them via env vars or a `.env` file. Do not add
  secrets to `config.py` or any committed file.

## Known-broken

- **DAVE voice reception.** Pycord 2.8.1's voice reception is broken by
  Discord's DAVE (End-to-End Encryption) protocol. The `/oc_voice` recording
  API emits a `RuntimeWarning` on every `start_recording` call and audio
  capture does not currently function on modern voice channels — a previous
  approach of patching the Pycord site-packages was abandoned as it did not
  work, and a replacement voice-capture solution is being sought. The TTS,
  STT, session-routing, and text-command parts are unaffected — only
  `/oc_voice` recording needs the replacement.

## Secrets

- No secrets are committed. All `BotConfig` defaults are empty strings / safe
  defaults. The user sets them via env vars (uppercase of the field name,
  auto-mapped by pydantic-settings) or a `.env` file in the run directory.
- Env-var overrides: `DISCORD_BOT_TOKEN` / `DISCORD_BOT_GUILD_ID` /
  `DISCORD_BOT_ALLOWED_CHANNEL_IDS` (JSON list) /
  `DISCORD_BOT_SESSION_CATEGORY_ID` / `OPENCODE_SERVER_URL` /
  `OPENCODE_SERVER_PASSWORD` / `OPENCODE_SERVER_USERNAME` /
  `OPENCODE_SERVE_ENABLED` / `OPENCODE_SERVE_PORT` / `OPENCODE_SERVE_HOSTNAME` / `OPENCODE_SERVE_CORS`
  (JSON list)   / `OPENCODE_SERVE_STARTUP_TIMEOUT` / `OPENCODE_SERVE_CWD` /
  `OC_SINGLETON_LOCK` /
  `OPENCODE_DEFAULT_MODEL` / `OPENCODE_ASSISTANT_MODEL` /
  `VOICE_MESSAGE_ENABLED` / `VOICE_MESSAGE_TRIGGER_CHANNEL_ID` /
  `VOICE_SILENCE_TIMEOUT_SECONDS` / `VOICE_CHUNK_SECONDS` /
  `VOICE_STT_PROVIDER` / `VOICE_STT_MODEL` / `VOICE_LOCAL_WHISPER_MODEL` /
  `VOICE_STT_HOTWORDS` / `VOICE_STT_PROMPT` / `VOICE_STT_REPLACEMENTS` /
  `VOICE_TTS_ENABLED` / `VOICE_TTS_MODEL` / `VOICE_TTS_VOICE` /
  `VOICE_TTS_SPEED` / `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE`
  / `SPEAKERS_DIR` / `SPEAKER_ID_ENABLED` / `SPEAKER_MATCH_THRESHOLD`
  / `HF_TOKEN`
  / `OPENAI_API_KEY` / `OLLAMA_API_URL` / `OLLAMA_AUTH_KEY` / `SLUG_MODEL` /
  `SLUG_TIMEOUT_SECONDS` / `COMULYTIC_ENABLED` / `COMULYTIC_JWT` /
  `COMULYTIC_REFRESH_TOKEN` / `COMULYTIC_API_BASE` / `COMULYTIC_WEB_BASE` /
  `COMULYTIC_AUDIO_PATH` / `COMULYTIC_POLL_INTERVAL_SECONDS` /
  `COMULYTIC_POLL_PAGE_SIZE` / `COMULYTIC_USER_AGENT` / `COMULYTIC_PLAN_TYPE` /
  `COMULYTIC_RELOGIN_WARN_DAYS` / `COMULYTIC_STATE_FILE` /
  `COMULYTIC_DISCORD_POINTER_CHANNEL_ID` / `COMULYTIC_QUESTION_TIMEOUT_SECONDS` /
  `COMULYTIC_QUESTION_POLL_INTERVAL_SECONDS` / `COMULYTIC_MAX_DURATION_HOURS` /
  `COMULYTIC_MAX_DURATION_MINUTES` / `COMULYTIC_MAX_DURATION_SECONDS` /
  `DASHBOARD_ENABLED` / `DASHBOARD_PORT` / `DASHBOARD_TOKEN`.
- Get the keys at: Discord bot token at
  https://discord.com/developers/applications (Bot tab), OpenAI key at
  https://platform.openai.com/api-keys, Ollama Cloud key at
  https://ollama.com.