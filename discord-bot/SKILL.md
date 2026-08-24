---
name: discord-bot
description: Two-way Discord gateway for opencode — /oc, /oc_plan, /oc_voice, /oc_talk slash commands with voice-channel support (faster-whisper local STT, optional OpenAI TTS)
slash: false
---

# opencode Discord Control Bot

A two-way Discord gateway for opencode. Slash commands create fresh opencode
sessions in fresh Discord channels, stream progress, and relay the final
response. Plain-text follow-ups in a session channel are forwarded to the
bound opencode session. Voice commands record spoken prompts and transcribe
them via faster-whisper (local, CTranslate2) or OpenAI Whisper (cloud).

## What this bot does

- **`/oc <prompt>`** — creates a fresh opencode session + a fresh Discord text
  channel (under the configured category), runs the prompt, streams progress,
  and posts the final assistant response in the channel. Subsequent plain-text
  messages in that channel are forwarded as follow-up prompts to the same
  session.
- **`/oc_plan <prompt>`** — same as `/oc` but runs the opencode `plan` agent
  (plan mode — outlines the change without executing).
- **`/oc_new`** — unbinds the current session channel (subsequent plain-text
  messages are ignored; next `/oc` creates a fresh session).
- **`/oc_session`** — shows the bound opencode session id for the current
  channel.
- **`/oc_abort`** — aborts the opencode session bound to the current channel.
- **`/oc_sessions`** — lists all active opencode sessions.
- **`/oc_voice <mode>`** — joins your voice channel, records your spoken
  prompt, transcribes it (faster-whisper local or OpenAI cloud), runs it
  through opencode, and (if TTS is enabled) speaks the response back.
- **`/oc_voice_stop`** — stops an in-progress `/oc_voice` recording.
- **`/oc_talk`** (with an audio/video attachment) — transcribes the attached
  audio and runs it as an opencode prompt.
- **`/oc_cleanup`** — deletes every text channel under the configured
  session category and clears the matching `SessionRouter` bindings, leaving
  the category itself intact. Requires the Manage Channels permission.
- **`/oc_setup`** — one-time guild setup: creates the "OpenCode Sessions"
  category plus the `voice-recordings` and `bot-commands` channels, writes
  their IDs + the guild id to `.env`, and reloads config live. Requires the
  Manage Channels permission. Refuses to run twice (clear the guild-specific
  fields in `.env` to re-run).
- **`/oc_help`** — posts an ephemeral summary of every command.

## How to install it

```bash
pip install git+https://github.com/jakestrouse00/opencode-discord-bot.git
```

This installs the `opencode_discord_bot` Python package and a
`opencode-discord-bot` console script. The bot runs as a standalone process
(separate from the opencode app you're controlling).

### System dependencies (NOT pip-installable)

- **Python 3.13+** — the bot targets Python 3.13.
- **ffmpeg** — must be on `PATH`. Used for `/oc_voice` and `/oc_talk` audio
  extraction + TTS playback. Install via `choco install ffmpeg` (Windows),
  `brew install ffmpeg` (macOS), or `apt install ffmpeg` (Linux).
- **opencode** — must be on `PATH` (the bot auto-spawns `opencode serve` as a
  child process on login). Install via `npm install -g opencode-ai` or
  download the binary from https://opencode.ai.

### GPU / CUDA (optional, faster local STT)

The default `WHISPER_DEVICE=cpu` needs none of this — skip ahead. Only
required when you set `WHISPER_DEVICE=cuda` to run faster-whisper on an
NVIDIA GPU (significant speedup for `medium`/`large` models). Probe with
`nvidia-smi`; non-NVIDIA GPUs are not supported by the CTranslate2 backend.

You need **CUDA 12.x** + **cuBLAS** + **cuDNN 9** (the shared runtime
libraries, NOT `nvcc`). Pair `WHISPER_DEVICE=cuda` with a GPU compute type:
`WHISPER_COMPUTE_TYPE=float16` (fastest, full GPU) or `int8_float16`
(lower VRAM, slightly slower). The CPU default `int8` is suboptimal on
`cuda`.

Install per platform — see `SETUP_GUIDE.md` "CUDA / GPU deps" for the
exact commands:
- **Linux (pip — easiest):** `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*` + export `LD_LIBRARY_PATH` to the two lib dirs (must be set in the shell you launch the bot from).
- **Windows:** the pip wheels do NOT ship CUDA DLLs. Download the NVIDIA-libs archive from
  [Purfview/whisper-standalone-win](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs)
  and place the extracted DLLs on `PATH`.
- **Docker:** base on `nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04` + the NVIDIA Container Toolkit.

Verify the GPU is visible: `python -c "import ctranslate2; print('cuda devices:', ctranslate2.get_cuda_device_count())"` — must print >0. `0` = the CUDA libs aren't found; re-check `LD_LIBRARY_PATH` (Linux) / `PATH` (Windows). If stuck on older CUDA/cuDNN, pin a compatible CTranslate2 (CUDA 12 + cuDNN 8 → `ctranslate2==4.4.0`; CUDA 11 + cuDNN 8 → `ctranslate2==3.24.0`).

## Required environment variables

| Variable | Required? | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | **Yes** | Discord bot token (https://discord.com/developers/applications) |
| `OPENCODE_SERVER_PASSWORD` | **Yes** | Any string — protects `opencode serve` with basic auth |
| `DISCORD_BOT_GUILD_ID` | No (0=global) | Guild id for instant slash-command sync |
| `OPENCODE_DEFAULT_MODEL` | No | Override the model for `/oc` + plain-text follow-ups. Empty = opencode default agent's frontmatter model wins. |
| `OPENCODE_ASSISTANT_MODEL` | No | Override the model for `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-msg trigger, Comulytic bridge. Empty = oc-assistant (Bobby) frontmatter model wins. |
| `OPENAI_API_KEY` | No | TTS + cloud STT fallback (skip if `voice_tts_enabled=false` AND `voice_stt_provider=local`) |
| `OLLAMA_AUTH_KEY` | No | LLM channel-name slugs (skip = regex fallback) |

Set these via environment variables or a `.env` file in the directory you run
the bot from. See `.env.example` (shipped in the package / repo) for the full
list including voice and faster-whisper settings.

## Install the oc-assistant (Bobby) agent (one-time, per target project)

The bot's `/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message trigger, and
Comulytic-bridge paths route prompts to opencode's `oc-assistant (Bobby)` agent. That
agent is **not** built into opencode — it lives in the target project's
`.opencode/agent/oc-assistant.md`. This package ships a generic, self-contained
copy you can install into any project:

```bash
# From the target project's root (the one opencode serve runs against):
python -m opencode_discord_bot.install_agent

# Or from anywhere, pointing at the project root:
python -m opencode_discord_bot.install_agent --dest /path/to/your/project
```

The agent lands at `<dest>/.opencode/agent/oc-assistant.md`. Run this once per
target project. See `SETUP_GUIDE.md` "Install the oc-assistant (Bobby) agent" for
details and the `--force` flag.

## How to run it

```bash
# Set required env vars (or fill in a .env file)
export DISCORD_BOT_TOKEN=<your token>
export OPENCODE_SERVER_PASSWORD=opencode-local-dev

# 1. Sync slash commands to your guild (one-shot; the bot does NOT auto-sync
#    on startup — auto_sync_commands=False). Required BEFORE /oc_setup will
#    appear in Discord.
python -m opencode_discord_bot.sync_commands --guild <your guild id>

# 2. Run the bot (it auto-spawns `opencode serve` on login)
python -m opencode_discord_bot

# Or use the console script
opencode-discord-bot

# 3. After /oc_setup writes the guild config (see below), stop the bot and
#    re-sync so every command is registered, then restart:
python -m opencode_discord_bot.sync_commands --guild <your guild id>
```

The bot starts the Discord gateway, auto-spawns `opencode serve` as a child
process (unless `OPENCODE_SERVE_ENABLED=false`), and registers slash commands
to the configured guild (or globally if `DISCORD_BOT_GUILD_ID=0`).

### Guild setup via `/oc_setup`

After the bot is running and the command surface is synced, invoke
`/oc_setup` in your server (requires the **Manage Channels** permission). It
does the guild-specific Discord setup for you in one click:

1. Creates a category **"OpenCode Sessions"** in the invoking guild.
2. Creates two text channels at the guild root (so `/oc_cleanup` won't
   wipe them): `voice-recordings` (→ `VOICE_MESSAGE_TRIGGER_CHANNEL_ID`)
   and `bot-commands` (→ `DISCORD_BOT_ALLOWED_CHANNEL_IDS`).
3. Writes the three created IDs + the guild id to `.env` atomically and
   reloads config live.
4. Refuses to run twice — if any of those three guild-specific fields is
   already set, it replies "already set up" and does nothing (clear them
   in `.env` manually to re-run).

After `/oc_setup`: stop the bot (Ctrl-C), re-run
`python -m opencode_discord_bot.sync_commands --guild <guild id>` so every
command is registered now that the guild config is written, then restart
the bot for normal use. See `SETUP_GUIDE.md` "/oc_setup — one-time guild
setup" for the full details.

## Comulytic bridge (optional, polling-based)

The Comulytic bridge routes Comulytic Note Pro recordings to opencode's
`oc-assistant (Bobby)` agent — polls Comulytic's cloud API for new recordings,
downloads each recording's audio, transcribes it LOCALLY via
faster-whisper (the same pipeline `/oc_talk` uses), and posts the
oc-assistant (Bobby) conversation to a Discord channel (when
`DISCORD_BOT_TOKEN` + `DISCORD_BOT_GUILD_ID` are set). It's disabled by
default and auto-spawns in-process when the bot starts with
`COMULYTIC_ENABLED=true` + `COMULYTIC_JWT` set — no separate launch
needed.

To enable it, set in `.env`:
```
COMULYTIC_ENABLED=true
COMULYTIC_JWT=<150-day access JWT captured from a login at web.comulytic.ai>
COMULYTIC_REFRESH_TOKEN=<365-day refresh JWT captured alongside it>
```

Capturing the JWT requires a browser DevTools Network capture of the
login exchange (the JWT is HS256, server-signed — the bridge cannot
self-sign it). See `SETUP_GUIDE.md` "Comulytic bridge → Capture the JWT
+ refresh token" for the exact steps. The standalone console script
`comulytic-bridge` (or `python -m opencode_discord_bot.bridge`) is a
manual override for running the bridge out-of-process.

## /oc_voice recording (broken)

Pycord 2.8.1's voice reception is broken by Discord's DAVE (End-to-End
Encryption) protocol on modern voice channels — `/oc_voice` recording does
not currently work, and a replacement voice-capture solution is being sought.
The recording API exists and is wired, but audio capture does not function.
The TTS, STT, session-routing, and text-command parts of the bot are
unaffected — only `/oc_voice` recording needs the replacement.

## Slash commands reference

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

## faster-whisper model options

The default `voice_stt_provider=local` uses faster-whisper (CTranslate2) for
100% local transcription — no cloud API, no per-request cost, privacy-
preserving. Model size is controlled by `VOICE_LOCAL_WHISPER_MODEL` /
`WHISPER_MODEL`:

| Model | Size | Speed | Accuracy |
|---|---|---|---|
| `tiny` | ~75MB | Fastest | Lowest |
| `base` | ~140MB | Fast | Good (CPU-feasible default) |
| `small` | ~480MB | Medium | Better |
| `medium` | ~1.5GB | Slow | High (default) |
| `large` | ~3GB | Slowest | Highest |

Device + compute type:
- `WHISPER_DEVICE=cpu` (default) / `cuda` (GPU)
- `WHISPER_COMPUTE_TYPE=int8` (CPU default) / `float16` (GPU) /
  `int8_float16` (GPU mixed)

The model downloads on first use and is cached for subsequent runs.