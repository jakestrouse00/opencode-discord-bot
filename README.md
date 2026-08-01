# opencode-discord-bot

A two-way Discord gateway for [opencode](https://opencode.ai). Control
opencode from Discord — run prompts, create plans, record voice, transcribe
audio — all via slash commands. Each `/oc` invocation creates a fresh
opencode session in a fresh Discord channel; subsequent plain-text messages
in that channel are forwarded as follow-up prompts.

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

After `/oc` or `/oc_plan` creates a session channel, any plain-text message in
that channel is forwarded as a follow-up prompt to the bound opencode session.

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