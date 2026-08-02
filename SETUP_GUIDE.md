# Setup Guide — opencode Discord Control Bot

This guide walks an AI agent (or a human following along) through the exact
sequence to install, configure, and run the opencode Discord control bot. The
bot is a two-way Discord gateway for opencode: `/oc` and `/oc_plan` create
fresh opencode sessions in fresh Discord channels, `/oc_voice` records spoken
prompts, `/oc_talk` transcribes audio attachments, and plain-text follow-ups
in a session channel are forwarded to the bound opencode session.

## Quick Setup Workflow

1. Install Python 3.13+ and ffmpeg (system dep, see below).
2. `pip install -r bot/requirements.txt` (or `pip install -e .` in the
   standalone repo).
3. Create a Discord application + bot, invite it to your server (see
   "Invite Bot to Discord").
4. Copy `bot/.env.example` to `.env`, fill in `DISCORD_BOT_TOKEN` and
   `OPENCODE_SERVER_PASSWORD` (required), plus optional voice/slug keys and
   model overrides.
5. Enable the **Message Content** privileged intent in the Discord Developer
   Portal (Bot -> Privileged Gateway Intents) — without it, plain-text
   follow-ups silently break.
6. **Install the bundled `plan-author` opencode agent** into the project the
   bot will run against: `python -m opencode_discord_bot.install_agent` (run
   from that project's root, or pass `--dest <project root>`). See "Install the
   plan-author agent" below. Skip this only if the target project already has
   its own `plan-author` agent you want to keep.
7. `python -m bot` (from the repo root, or any directory if using the
   standalone package).
8. In Discord, use `/oc <your prompt>` — the bot creates a channel, runs the
   opencode session, and posts the response there.

## Required Dependencies

### Python 3.13+
The bot targets Python 3.13+ (matches the orchestrator repo). On 3.13+ the
stdlib `audioop` module was removed, so `audioop-lts` is installed
automatically (listed in `requirements.txt`).

### pip packages
```bash
pip install -r bot/requirements.txt
```
Installs: `py-cord[voice]` (Discord gateway, Pycord 2.8.1), `httpx` (REST
client), `pydantic-settings` (config), `PyNaCl` (voice crypto),
`audioop-lts` (Python 3.13 audio dep), `faster-whisper` + `ctranslate2`
(local STT — used by `/oc_voice`, `/oc_talk`, voice messages, AND the
Comulytic bridge), `openai` (TTS + cloud STT fallback).

### ffmpeg (system dependency — NOT pip-installable)
`ffmpeg` must be on your `PATH`. The bot uses it for:
- `/oc_voice` audio extraction (converts recorded WAV chunks for Whisper).
- `/oc_talk` attachment audio extraction (extracts the audio track from
  video files, converts non-WAV audio to 16kHz mono WAV).
- TTS playback (`discord.FFmpegPCMAudio` plays the synthesized MP3).

Install via:
- **Windows:** `choco install ffmpeg` or `scoop install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux:** `apt install ffmpeg` / `dnf install ffmpeg` / etc.

Verify: `ffmpeg -version`

## Secrets & Configuration to Collect

| Secret | Required? | Where to get it | Used for |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | **Required** | https://discord.com/developers/applications (Bot tab) | Gateway login |
| `OPENCODE_SERVER_PASSWORD` | **Required** | Any string you choose | `opencode serve` basic auth |
| `DISCORD_BOT_GUILD_ID` | Optional (0 = global) | Developer Mode -> right-click server -> Copy ID | Slash-command sync target |
| `OPENCODE_DEFAULT_MODEL` | Optional | Your provider's model id (e.g. `ollama-cloud/glm-5.2`, `anthropic/claude-sonnet-4`, `openai/gpt-5`) | Override the model for `/oc` + plain-text follow-ups. Empty = the opencode default agent's frontmatter `model:` wins. |
| `OPENCODE_PLAN_AUTHOR_MODEL` | Optional | Same as above | Override the model for `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message trigger, and the Comulytic bridge (all `agent="plan-author"`). Empty = the plan-author agent's frontmatter model wins. |
| `OPENAI_API_KEY` | Optional | https://platform.openai.com/api-keys | TTS + cloud STT fallback (skip if `voice_tts_enabled=false` AND `voice_stt_provider=local`) |
| `OLLAMA_AUTH_KEY` | Optional | https://ollama.com | LLM channel-name slugs (skip = regex fallback) |

**Note on `OPENAI_API_KEY`:** it is needed for **TTS** (text-to-speech
playback in voice channels), not for local STT. The default
`voice_stt_provider=local` uses `faster-whisper` (CTranslate2, 100% local, no
OpenAI key). You only need `OPENAI_API_KEY` for STT if you set
`voice_stt_provider=openai` or `auto` (the latter falls back to cloud on
local failure).

## Create `.env` File

Copy `bot/.env.example` to `.env` (in the directory you run the bot from) and
fill in the values. At minimum:

```env
DISCORD_BOT_TOKEN=<your bot token>
OPENCODE_SERVER_PASSWORD=opencode-local-dev
```

For voice (local STT, no OpenAI key needed):
```env
VOICE_STT_PROVIDER=local
VOICE_LOCAL_WHISPER_MODEL=base
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

For TTS (optional):
```env
VOICE_TTS_ENABLED=true
OPENAI_API_KEY=<your openai key>
```

For LLM-generated channel-name slugs (optional):
```env
OLLAMA_API_URL=https://ollama.com/v1
OLLAMA_AUTH_KEY=<your ollama key>
SLUG_MODEL=gpt-oss:20b-cloud
```

For model overrides (optional — empty = each opencode agent's own frontmatter
model wins):
```env
# Model for /oc + plain-text follow-ups (agent=None):
OPENCODE_DEFAULT_MODEL=ollama-cloud/glm-5.2
# Model for /oc_plan, /oc_voice, /oc_talk, voice-msg trigger, Comulytic bridge:
OPENCODE_PLAN_AUTHOR_MODEL=anthropic/claude-sonnet-4
```

## Install the plan-author agent

The bot's `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message trigger, and
Comulytic-bridge paths all route prompts to opencode's `plan-author` agent.
That agent is **NOT** built into opencode itself — it lives in the target
project's `.opencode/agent/plan-author.md`. If the project doesn't have one,
those paths will fail agent resolution on the opencode server side.

This package ships a **fully generic, self-contained** plan-author agent
(`opencode_discord_bot/agent/plan-author.md`) that works in any project — it
reads the target project's own `AGENTS.md` if present, writes only to
`.opencode/plans/`, and is compatible with the `change-outline` skill's
resume/execute flow if that skill is also installed.

Install it into the project the bot will run against:

```bash
# From the target project's root directory (the one opencode serve runs in):
python -m opencode_discord_bot.install_agent

# Or from anywhere, pointing at the project root:
python -m opencode_discord_bot.install_agent --dest /path/to/your/project

# Overwrite an existing plan-author.md without prompting:
python -m opencode_discord_bot.install_agent --force
```

By default the install prompts before overwriting an existing
`plan-author.md` so a project that has its own customized agent isn't silently
clobbered. The agent lands at `<dest>/.opencode/agent/plan-author.md`.

**Why this matters with `OPENCODE_SERVE_CWD`:** the opencode server resolves
agents relative to its working directory (the `opencode_serve_cwd` setting,
which defaults to the bot's launch dir). If your `.env` lives in a subdirectory
but your project root (with `.opencode/agent/`) lives elsewhere, set
`OPENCODE_SERVE_CWD` to the project root **and** run `install_agent --dest`
against the same project root — otherwise the server won't find the agent
even though you installed it.

## Invite Bot to Discord

1. Go to https://discord.com/developers/applications and create a new
   application (or use an existing one).
2. Navigate to the **Bot** tab. Click **Reset Token** to generate a token.
   Copy it — this is your `DISCORD_BOT_TOKEN`.
3. Scroll down to **Privileged Gateway Intents** and enable **Message Content
   Intent** (required for plain-text follow-ups to work). Save changes.
4. Navigate to **OAuth2 -> URL Generator**. Select the `bot` and
   `applications.commands` scopes. Select the permissions your bot needs
   (at minimum: Send Messages, Manage Channels — it creates session channels,
   Read Message History — to edit the "Working…" progress message, Connect +
   Speak — for `/oc_voice`).
5. Open the generated URL in your browser to invite the bot to your server.

## Common Gotchas

### Pycord vs discord.py
This bot uses **Pycord** (`py-cord[voice]` 2.8.1), NOT `discord.py`. Pycord is
a fork that provides the voice recording/sinks API (`start_recording` /
`discord.sinks.Sink`) that `discord.py` removed in 2.4.0 and never restored.
Do not install `discord.py` alongside Pycord — they conflict.

### Missing ffmpeg
If `/oc_voice` or `/oc_talk` fails with an `ffmpeg` error, ffmpeg is not on
your `PATH`. Install it (see "Required Dependencies" above) and restart the
bot. The `ffmpeg -version` command should work in your terminal.

### faster-whisper model download
The first time the bot uses local STT (`voice_stt_provider=local`), it
downloads the Whisper model (e.g. `medium` ~1.5GB). This happens once and is
cached. Subsequent startups load from disk. If you're on a slow connection,
use a smaller model (`tiny` ~75MB, `base` ~140MB) for the first run.

### /oc_voice recording (broken)
Pycord 2.8.1's voice reception is broken by Discord's DAVE (End-to-End
Encryption) protocol on modern voice channels. The `/oc_voice` recording API
emits a `RuntimeWarning` on every `start_recording` call and audio capture does
not currently function — a replacement voice-capture solution is being sought.
The TTS, STT, session-routing, and text-command parts of the bot are
unaffected — only `/oc_voice` live recording needs the replacement. See
AGENTS.md "DAVE voice reception" (Known-broken) for context.

### opencode serve not running
The bot auto-spawns `opencode serve` as a child process on login (via
`OpencodeBot.on_connect`). If you see "opencode serve: binary not found",
install opencode (`npm install -g opencode-ai` or download the binary) so it's
on your `PATH`. If you see "server did not become healthy", the port may be in
use (see below) or `OPENCODE_SERVER_PASSWORD` is wrong.

### Missing privileged intents
If plain-text follow-ups in a session channel produce no response (the bot
seems to ignore them), the **Message Content** privileged intent is not
enabled. Go to the Discord Developer Portal -> Bot -> Privileged Gateway
Intents -> enable **Message Content Intent** -> Save. Restart the bot. The
`message.content` field is empty without this intent, so follow-ups silently
break.

### `.sessions.json` permissions
The bot persists channel->session bindings to a JSON file
(`.opencode-discord-bot-sessions.json` in the standalone package, or
`bot/.sessions.json` in the in-place layout). If the bot can't write to it
(permissions), bindings are lost on restart but the bot still runs. The file
is gitignored — it contains only session ids, no secrets.

### Port 4096 in use
`opencode serve` defaults to port 4096. If another process is using it, the
server fails to bind. Either kill the other process or set
`OPENCODE_SERVE_PORT=<other port>` (and `OPENCODE_SERVER_URL` to match) in
`.env`.

### Slash commands not appearing
If `/oc` doesn't show up in Discord after the bot starts:
- `DISCORD_BOT_GUILD_ID=0` means global sync, which can take up to 1 hour to
  propagate. Set `DISCORD_BOT_GUILD_ID=<your server id>` for instant guild
  sync.
- Run `python -m bot.sync_commands --guild <your server id>` to force a
  one-off sync without starting the gateway. **This is the only way commands
  are pushed** — the bot does NOT auto-sync on startup (auto-sync was
  disabled because it pushed a global copy on every login, which combined
  with the guild-scoped sync produced duplicate entries in the Discord UI).
  Re-run this after any change to the slash-command surface.
- Verify the bot has `applications.commands` scope in your server (OAuth2 URL
  Generator).
- If you see duplicate commands after migrating from the old auto-sync path,
  delete the orphaned global set once:
  `python -m bot.sync_commands --guild 0` (with `commands=[]`) or call
  `await bot.sync_commands(commands=[], guild_ids=None)` after `login()`.

### Voice on Windows
`ffmpeg` on Windows installs as a `.exe` (works directly). Pycord voice on
Windows needs `PyNaCl` (installed automatically via `py-cord[voice]`).
`/oc_voice` recording does not currently work on modern Discord voice channels
(see "/oc_voice recording (broken)" above).

## Verification Checklist

After setup, verify each piece:

- [ ] `python -c "from bot.commands import OpencodeBot; print('ok')"` — bot
  imports without error.
- [ ] `ffmpeg -version` — ffmpeg is on PATH.
- [ ] `python -c "import faster_whisper; print('ok')"` — local STT dep
  installed (skip if you'll only use cloud STT).
- [ ] `python -m bot` starts without error and logs "Starting opencode Discord
  bot".
- [ ] `/oc hello` in Discord creates a channel and responds.
- [ ] (Optional) `/oc_voice` joins a voice channel and transcribes your speech.
- [ ] (Optional) `/oc_talk` with an audio attachment transcribes it.

## Comulytic bridge (optional, polling-based)

The Comulytic bridge is a separate long-running process that polls Comulytic's
cloud API for new Note Pro recordings, downloads each recording's audio, and
transcribes it LOCALLY via faster-whisper (the same pipeline `/oc_talk` uses),
then routes the transcript to opencode's `plan-author` agent — no manual
Discord upload, no phone-side automation, no Comulytic cloud ASR. It runs
independently of the Discord bot (`python -m opencode_discord_bot`) but is
also auto-spawned in-process by the bot when `COMULYTIC_ENABLED=true` +
`COMULYTIC_JWT` are set (see the bot's `AGENTS.md`).

### Enable the bridge

The bridge is **disabled by default**. To enable it, set in your `.env`:

```
COMULYTIC_ENABLED=true
COMULYTIC_JWT=<paste the 150-day access JWT here>
```

When `COMULYTIC_ENABLED` is unset/false, the bridge exits immediately at
startup with a clear message — the feature is fully OFF. To disable it
again, just remove (or set to `false`) the `COMULYTIC_ENABLED` line in your
`.env` and restart the bridge.

### Capture the JWT + refresh token

The bridge needs a Bearer JWT captured from a real login at
`web.comulytic.ai`. The JWT is HS256 (server-held symmetric secret — the
bridge cannot self-sign it) and has a **150-day TTL** (`expiresIn: 12960000`
in the login response). A **365-day `refreshToken`** is also returned
alongside it; persist it too (the refresh *call* is a capture gap — see
"JWT expiry" below).

**Preferred (scriptable, non-interactive) — email+password:**

1. Sign in at `web.comulytic.ai` using email+password.
2. Open browser DevTools → Network tab BEFORE clicking sign-in.
3. Perform the login.
4. Find the `POST /api/kirby/v1/auth/login/email` request (preceded by
   `POST /api/kirby/v1/auth/exist/email` pre-check).
5. Copy `data.accessToken` (the 150-day access JWT) → `COMULYTIC_JWT`.
6. Copy `data.refreshToken` (the 365-day refresh JWT) →
   `COMULYTIC_REFRESH_TOKEN`.
7. Copy `data.user.userId` and verify it matches the JWT `sub` claim.

**Alternative (Apple social, browser-only):** "Continue with Apple"
triggers `POST /api/kirby/v1/auth/exist/socialAccount` (pre-check) then
`POST /api/kirby/v1/auth/social/login`. Same response shape
`{data:{accessToken, refreshToken, ...}}`. Apple id_tokens have a 1-day TTL
the bridge cannot mint autonomously, so prefer the email path for automation.

### Run the bridge

```
COMULYTIC_ENABLED=true COMULYTIC_JWT=<...> COMULYTIC_REFRESH_TOKEN=<...> comulytic-bridge
```

(or `python -m opencode_discord_bot.bridge`). The bridge spawns its own
`opencode serve` subprocess (reuses a running one if the Discord bot is
running simultaneously — no port conflict). It logs to stderr in the same
format as the bot.

### How it works

- **Polling:** polls `POST /api/kirby/v2/note/paging` every
  `COMULYTIC_POLL_INTERVAL_SECONDS` (default 60s). A cheap `pageSize:1`
  change-detect call returns `total` + the newest `noteId`; full enumeration
  only when `total` changes. Lower to 15-30s if latency matters.
- **Bootstrap-seen:** the first run marks all currently-existing recordings
  as seen WITHOUT processing them; only recordings created AFTER bridge
  start get routed (no one-time backlog flood).
- **Audio-delivery predicate:** audio download is gated on
  `hasCloudAudio == true AND audioAccess == "public"` (an AI-pipeline-agnostic
  audio-delivery signal). A recording can be audio-delivered before its
  Comulytic cloud transcript is ready (or even if that transcript fails) —
  the bridge no longer cares, since it transcribes locally (see below).
- **Transcription is LOCAL Whisper only:** the bridge NEVER consults
  Comulytic's cloud ASR (`queryTranscribeResult` / `asrResultVO`). For every
  audio-delivered recording it downloads the audio and runs the SAME
  pipeline `/oc_talk` uses — `voice.extract_audio_to_wav` (ffmpeg → mono
  16kHz WAV) + `voice.transcribe_audio`. `transcribe_audio` dispatches on
  `voice_stt_provider`: `"local"` (default) = in-process faster-whisper
  CTranslate2 (no cloud API, no per-request cost, privacy-preserving),
  `"openai"` = cloud Whisper API, `"auto"` = local first, cloud fallback.
  The old `COMULYTIC_AUDIO_FALLBACK` flag (cloud ASR primary, local Whisper
  fallback) is GONE — local Whisper is the sole path, so the flag is moot.
- **Audio download paths:** default **Path B** (cookie-auth proxy at
  `web.comulytic.ai/api/note/audio-range/{noteId}` — stable, no per-URL
  expiry, only the JWT cookie rotates) with **Path A** (pre-signed S3 via
  `noteDetail`, 48h TTL, re-mint each cycle) as fallback. Override via
  `COMULYTIC_AUDIO_PATH=presigned` to force Path A.

### JWT expiry

The bridge warns `COMULYTIC_RELOGIN_WARN_DAYS` (default 1) before the
access-JWT `exp`. The 365-day `refreshToken` exists and is persisted, BUT the
refresh *call* (endpoint path/body) was NOT captured in the HAR investigation
— until re-captured, the bridge treats the access JWT as non-refreshable and
re-runs the full login (`/auth/login/email` preferred) before `exp`. On
expiry all calls 401 and the bridge effectively goes silent (polls fail,
logs errors, no new recordings routed). Schedule a calendar reminder for
~1 day before `exp` (the bridge logs the expiry date on startup). A future
HAR capturing the refresh exchange would close this gap and enable proactive
refresh at `exp − 24h` with no code changes.

### `acw_tc` cookie-jar behavior

The `acw_tc` cookie is a passive challenge token (Aliyun WAF), 30-min Max-Age,
HttpOnly, auto-minted on every qualifying API response even when none was
pre-sent. The bridge's `httpx.AsyncClient` cookie jar captures + resends it
automatically (no manual cookie handling). If the bridge idles >30 min, the
next request gets a fresh `Set-Cookie` automatically.

### Rate limits

Effectively unbounded (`x-ratelimit-limit: 999999999`, no 429/`Retry-After`
observed in capture), but the bridge honors `x-ratelimit-remaining`/`reset`
+ `Retry-After` defensively in case real limits appear. The 60s default
cadence is conservative.

### Push mode (follow-up, NOT implemented)

The bridge probes `GET /api/openapi/v1/api-keys/developer-tab/visibility`
once on startup. If `.data.visible == true`, it logs a NOTICE that push mode
is available for this account (inbound webhook delivery instead of polling).
Implementing the push receiver (inbound FastAPI endpoint + HMAC verification
+ `fastapi`/`uvicorn` deps + webhook registration) is a follow-up plan — the
polling bridge continues to run regardless of the probe result.

### Comulytic bridge → Discord channel (mirrors `/oc_talk`)

When `DISCORD_BOT_TOKEN` + `DISCORD_BOT_GUILD_ID` are set (the same values
the main bot uses — the bridge reads them from the same `.env`), the bridge
additionally creates a Discord text channel for each routed recording and
posts the plan-author conversation there — just like `/oc_talk`, but with
Comulytic as the audio source instead of a Discord attachment.

**Flow:** when a new Comulytic recording's audio is downloaded and locally
transcribed (faster-whisper), the bridge:

1. Creates an opencode session titled `comulytic-<shortNoteId>`.
2. Creates a Discord text channel under `DISCORD_BOT_SESSION_CATEGORY_ID`
   (the same category the main bot uses for `/oc` / `/oc_talk` channels), with
   an initial regex slug derived from the transcript (e.g.
   `comulytic-1bb61861`), topic `opencode comulytic session <sid> (note <id>)`.
3. Binds the channel to the session in a SEPARATE persistence file
   (`.opencode-discord-bridge-sessions.json`, gitignored) so the bridge and
   the main bot don't clobber each other's writes.
4. Posts the transcript in the channel under a `**Transcribed prompt:**`
   header.
5. Fires a fire-and-forget LLM slug rename (one short chat completion on the
   small cloud model via `SLUG_MODEL` + `OLLAMA_AUTH_KEY`) to upgrade the
   channel name from the regex slug to a real LLM-generated slug.
6. Posts a `Working on session <sid>…` progress message (throttled progress
   edits while plan-author runs).
7. Sends the transcript to `plan-author` (optionally prepended with a
   `[PLAN_TYPE_PRESELECTED: ...]` directive when `COMULYTIC_PLAN_TYPE` is set).
8. Surfaces any plan-author clarifying questions as plain-text prompts in the
   channel (numbered options + a timeout) and polls
   `GET /channels/{id}/messages` for the user's plain-text reply. The user
   types a number or any text; the bridge parses it and calls
   `reply_question` / `reject_question` to unblock the agent turn.
9. Posts the final plan-author response in the channel (chunked into
   ≤2000-char pieces).
10. Optionally posts a `Created <#channelId>` pointer to
    `COMULYTIC_DISCORD_POINTER_CHANNEL_ID` (a "firehose" channel to watch for
    new bridge activity; 0 = no pointer).

**No Pycord, no gateway.** The bridge talks to Discord via raw REST
(`POST /guilds/{id}/channels`, `POST /channels/{id}/messages`, etc.) using the
bot token in the `Authorization` header. It does NOT connect to the Discord
gateway — Discord allows only one gateway connection per bot token, so the
bridge connecting would kick the main bot's connection into a reconnect loop.
Raw REST is safe to run alongside the main bot (REST rate limits are
per-token, but channel creation + a few messages per recording is low-volume).

**Bot permissions:** the bot needs `Manage Channels` (to create the text
channel), `Send Messages` (to post the transcript + response), `Read Message
History` (to poll for the user's plain-text replies to questions), and
`Manage Messages` (to edit the progress message) in the
`DISCORD_BOT_SESSION_CATEGORY_ID` category. These are the same permissions
the main bot already needs for `/oc` / `/oc_talk`.

**Plain-text follow-ups are a follow-on.** The bridge binds its channels in
a separate file that the main bot doesn't read, so plain-text follow-ups in
bridge-created channels do NOT route through the main bot's `on_message`
follow-up path. To follow up in a bridge-created channel, use `/oc_new` (or
re-run the recording). Wiring the bridge's channels into the main bot's
follow-up path is a future enhancement (the bridge would need to either poll
its channels for new messages, or share the SessionRouter file with locking).

**Graceful degradation.** If `DISCORD_BOT_TOKEN` is empty or
`DISCORD_BOT_GUILD_ID` is 0, the bridge routes to plan-author and LOGs the
response only (the original behavior). A WARNING is logged on startup. No
Discord channel is created. This is the default if you only set the Comulytic
env vars and not the Discord ones.

**New env vars:**
- `COMULYTIC_DISCORD_POINTER_CHANNEL_ID` (default 0): channel id to post the
  `Created <#channel>` pointer in. 0 = no pointer.
- `COMULYTIC_QUESTION_TIMEOUT_SECONDS` (default 300): how long to wait for
  the user's plain-text reply to a plan-author clarifying question before
  rejecting it (unblocking the agent). The main bot uses buttons that wait
  indefinitely; plain-text polling needs a ceiling.
- `COMULYTIC_QUESTION_POLL_INTERVAL_SECONDS` (default 2.0): poll cadence for
  `GET /question` + `GET /permission` while plan-author runs.

## Troubleshooting Commands

```bash
# Check the bot token is set (without revealing it)
python -c "from bot.config import config; print('token set' if config.discord_bot_token else 'EMPTY')"

# Check opencode is on PATH
python -c "import shutil; print(shutil.which('opencode') or 'NOT FOUND')"

# Force sync slash commands to a specific guild (no gateway, no serve)
python -m bot.sync_commands --guild <your guild id>

# Check the opencode server is reachable
curl -u opencode:<your password> http://127.0.0.1:4096/global/health

# Verify faster-whisper loads (downloads the model on first run)
python -c "from faster_whisper import WhisperModel; m=WhisperModel('tiny', device='cpu', compute_type='int8'); print('ok')"
```