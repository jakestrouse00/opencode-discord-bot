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
   `OPENCODE_SERVER_PASSWORD` (required), plus optional voice/slug keys.
5. Enable the **Message Content** privileged intent in the Discord Developer
   Portal (Bot -> Privileged Gateway Intents) — without it, plain-text
   follow-ups silently break.
6. `python -m bot` (from the repo root, or any directory if using the
   standalone package).
7. In Discord, use `/oc <your prompt>` — the bot creates a channel, runs the
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
(local STT), `openai` (TTS + cloud STT fallback).

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