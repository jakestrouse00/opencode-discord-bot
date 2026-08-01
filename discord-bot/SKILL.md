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

## Required environment variables

| Variable | Required? | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | **Yes** | Discord bot token (https://discord.com/developers/applications) |
| `OPENCODE_SERVER_PASSWORD` | **Yes** | Any string — protects `opencode serve` with basic auth |
| `DISCORD_BOT_GUILD_ID` | No (0=global) | Guild id for instant slash-command sync |
| `OPENAI_API_KEY` | No | TTS + cloud STT fallback (skip if `voice_tts_enabled=false` AND `voice_stt_provider=local`) |
| `OLLAMA_AUTH_KEY` | No | LLM channel-name slugs (skip = regex fallback) |

Set these via environment variables or a `.env` file in the directory you run
the bot from. See `.env.example` (shipped in the package / repo) for the full
list including voice and faster-whisper settings.

## How to run it

```bash
# Set required env vars (or fill in a .env file)
export DISCORD_BOT_TOKEN=<your token>
export OPENCODE_SERVER_PASSWORD=opencode-local-dev

# Run the bot (it auto-spawns `opencode serve` on login)
python -m opencode_discord_bot

# Or use the console script
opencode-discord-bot
```

The bot starts the Discord gateway, auto-spawns `opencode serve` as a child
process (unless `OPENCODE_SERVE_ENABLED=false`), and registers slash commands
to the configured guild (or globally if `DISCORD_BOT_GUILD_ID=0`).

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