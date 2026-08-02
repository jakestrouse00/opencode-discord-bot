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
  optional `OPENCODE_DEFAULT_MODEL` / `OPENCODE_PLAN_AUTHOR_MODEL` (override
  the opencode model for `/oc`/follow-ups vs `/oc_plan`/`/oc_voice`/`/oc_talk`/
  bridge) via env vars or a `.env` file in the run directory. See `.env.example`
  for the full list.
- **Install the `plan-author` agent:** the bot's `/oc_plan`, `/oc_voice`,
  `/oc_talk`, voice-message trigger, and Comulytic-bridge paths route to
  opencode's `plan-author` agent, which is **not** built into opencode — it
  lives in the target project's `.opencode/agent/plan-author.md`. This package
  ships a generic, self-contained copy. Run
  `python -m opencode_discord_bot.install_agent` (from the target project's
  root, or `--dest <project root>`) to install it. See `SETUP_GUIDE.md`
  "Install the plan-author agent" for details.
- **Run:** `python -m opencode_discord_bot` (or `opencode-discord-bot`). The
  bot starts the Discord gateway, auto-spawns `opencode serve` as a child
  process (unless `OPENCODE_SERVE_ENABLED=false`). **Slash commands are NOT
  auto-synced on startup** — `auto_sync_commands=False` is set in
  `OpencodeBot.__init__` to prevent Pycord's `on_connect` from pushing a
  global copy of every command on every login (which, combined with the
  guild-scoped commands pushed by `sync_commands.py`, produced duplicate
  entries in the Discord UI). Sync commands explicitly via
  `python -m opencode_discord_bot.sync_commands --guild <id>` after any
  change to the slash-command surface.
- **Privileged intent:** the **Message Content** gateway intent MUST be
  enabled in the Discord Developer Portal (Bot -> Privileged Gateway Intents)
  for plain-text follow-ups to work. The bot sets `intents.message_content =
  True` in `OpencodeBot.__init__`; the portal toggle is separate. Without it,
  `message.content` is always empty and follow-ups silently break.
- **No tests, no linter, no type-checker, no build step** are configured in
  this repo. The only meaningful automated check is the Python import sanity
  check: `python -c "from opencode_discord_bot.commands import OpencodeBot;
  print('ok')"`.

## Architecture

- **Package layout:** `src/opencode_discord_bot/` (pip-installable, importable
  as `opencode_discord_bot`). The `src/` layout means the package is only
  importable after `pip install -e .` (or adding `src/` to `PYTHONPATH` for
  development). Top-level files (`index.json`, `discord-bot/SKILL.md`,
  `AGENTS.md`, `README.md`, `pyproject.toml`, `.env.example`) sit at the repo
  root, NOT inside the package.
- **`OpencodeBot(discord.Bot)`** — the main bot class
  (`opencode_discord_bot/commands.py`). Owns the slash-command surface
  (`/oc`, `/oc_plan`, `/oc_new`, `/oc_session`, `/oc_abort`, `/oc_sessions`,
  `/oc_voice`, `/oc_voice_stop`, `/oc_talk`, `/oc_cleanup`) + the plain-text follow-up path
  (`on_message`). Each `/oc` or `/oc_plan` invocation creates a fresh opencode
  session AND a fresh Discord text channel under the configured category
  (`discord_bot_session_category_id`), then posts the response there.
  Subsequent plain-text messages in that session channel are forwarded to the
  bound opencode session as follow-up prompts. The bot ignores messages in
  channels it did not create.
  `/oc_cleanup` is the one destructive maintenance command: it deletes every
  text channel under `discord_bot_session_category_id` and clears the matching
  `SessionRouter` bindings (via `router.reset`), leaving the category itself
  intact for reuse. It requires the Manage Channels permission (gated in the
  callback via `ctx.author.guild_permissions.manage_channels`) and is intended
  for cleaning up the server between test sessions. Discovery is strictly
  category-scoped (`category.text_channels`), so allowlisted command channels,
  voice channels, and user-created channels are never enumerated or deleted.
- **`OpencodeClient`** (`opencode_client.py`) — async `httpx` wrapper over
  the opencode server REST API (sessions, messages, questions, permissions,
  events). No official Python SDK exists (the SDK is JS/TS-only), so this is
  a thin typed surface over the documented REST endpoints. Auth: basic auth
  via `OPENCODE_SERVER_PASSWORD` env var. **Model overrides:**
  `send_prompt_async` / `send_message` resolve the model id from
  `config.opencode_default_model` (for `agent=None` — `/oc` + plain-text
  follow-ups) or `config.opencode_plan_author_model` (for `agent="plan-author"`
  — `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message trigger, Comulytic
  bridge) via `_resolve_model`, and include it in the POST body only when
  non-empty. Both empty (the default) = each opencode agent's own frontmatter
  `model:` field wins (the historical behavior). No call site passes `model`
  explicitly — the override flows through transparently.
- **Bundled `plan-author` agent** (`opencode_discord_bot/agent/plan-author.md`)
  — a fully generic, self-contained Plan Author subagent shipped inside the
  package. The bot routes plan-author prompts to opencode's `plan-author`
  agent, which the target project must have installed at
  `.opencode/agent/plan-author.md`. The bundled copy is project-agnostic (it
  reads the target project's own `AGENTS.md` if present, writes only to
  `.opencode/plans/`, and is compatible with the `change-outline` skill if
  that's installed). Install it via `python -m opencode_discord_bot.install_agent`
  (see `install_agent.py`). Shipped in the wheel via hatchling `force-include`.
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
  bug).
- **`SessionRouter`** (`session_router.py`) — maps Discord channel id ->
  opencode session id, persisted to `.opencode-discord-bot-sessions.json`
  (gitignored). One persistent opencode session per Discord channel. Loaded
  on construction, saved after each new binding.
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
  `1533242090862149842`) → new plan-author session via
  `_run_talk_from_message`; (d) else ignored. Gated by `voice_message_enabled`
  (config, default True). No `plan_type` directive is sent for voice-message
  new sessions (no slash-option UI) — the `plan-author` agent classifies from
  the transcript. No new deps; reuses the `/oc_talk` pipeline verbatim. Does
  NOT touch `/oc_voice` or the DAVE-broken sinks path.
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
- **Comulytic bridge → Discord channel** (`bridge.py:route_to_plan_author` +
  `discord_rest.py` + `bridge_questions.py` + `text_utils.py`): when
  `discord_bot_token` + `discord_bot_guild_id` are set, the bridge creates a
  Discord text channel for each routed Comulytic recording (mirrors
  `/oc_talk`), posts the transcript, fires an LLM slug rename, sends to
  `plan-author`, surfaces clarifying questions as plain-text prompts (polling
  `GET /channels/{id}/messages` for the user's reply via the REST-based
  `poll_pending_requests_rest`), and posts the final response. Uses **raw
  Discord REST via `DiscordRest`** (no Pycord, no gateway — safe to run
  alongside the main bot, which owns the gateway session). Binds channels in
  a **separate** persistence file `.opencode-discord-bridge-sessions.json`
  (NOT `.opencode-discord-bot-sessions.json`) so the bridge's channel bindings
  don't clobber the main bot's. `text_utils.py` is the Pycord-free shared module for
  `_split_message`/`_extract_text`/`_final_assistant_text`/`_slugify_prompt`
  (imported by both `commands.py` and `bridge.py`). When Discord isn't
  configured, `route_to_plan_author` falls back to log-only.
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
  `OPENCODE_SERVER_PASSWORD` / `OPENCODE_SERVE_ENABLED` /
  `OPENCODE_SERVE_PORT` / `OPENCODE_SERVE_HOSTNAME` / `OPENCODE_SERVE_CORS`
  (JSON list)   / `OPENCODE_SERVE_STARTUP_TIMEOUT` / `OPENCODE_SERVE_CWD` /
  `OPENCODE_DEFAULT_MODEL` / `OPENCODE_PLAN_AUTHOR_MODEL` /
  `VOICE_MESSAGE_ENABLED` / `VOICE_MESSAGE_TRIGGER_CHANNEL_ID` /
  `VOICE_SILENCE_TIMEOUT_SECONDS` / `VOICE_CHUNK_SECONDS` /
  `VOICE_STT_PROVIDER` / `VOICE_STT_MODEL` / `VOICE_LOCAL_WHISPER_MODEL` /
  `VOICE_TTS_ENABLED` / `VOICE_TTS_MODEL` / `VOICE_TTS_VOICE` /
  `VOICE_TTS_SPEED` / `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE`
  / `OPENAI_API_KEY` / `OLLAMA_API_URL` / `OLLAMA_AUTH_KEY` / `SLUG_MODEL` /
  `SLUG_TIMEOUT_SECONDS` / `COMULYTIC_ENABLED` / `COMULYTIC_JWT` /
  `COMULYTIC_REFRESH_TOKEN` / `COMULYTIC_API_BASE` / `COMULYTIC_WEB_BASE` /
  `COMULYTIC_AUDIO_PATH` / `COMULYTIC_POLL_INTERVAL_SECONDS` /
  `COMULYTIC_POLL_PAGE_SIZE` / `COMULYTIC_USER_AGENT` / `COMULYTIC_PLAN_TYPE` /
  `COMULYTIC_RELOGIN_WARN_DAYS` / `COMULYTIC_STATE_FILE` /
  `COMULYTIC_DISCORD_POINTER_CHANNEL_ID` / `COMULYTIC_QUESTION_TIMEOUT_SECONDS` /
  `COMULYTIC_QUESTION_POLL_INTERVAL_SECONDS`.
- Get the keys at: Discord bot token at
  https://discord.com/developers/applications (Bot tab), OpenAI key at
  https://platform.openai.com/api-keys, Ollama Cloud key at
  https://ollama.com.