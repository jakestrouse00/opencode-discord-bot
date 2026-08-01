"""Standalone settings for the opencode Discord control bot.

`BotConfig` holds ONLY the fields the bot package (`bot/`) reads, decoupled
from `core.settings.ExecutionSettings` so the bot can be imported, installed,
and run without dragging the orchestrator's `core/` package along (which
carries 20+ unrelated orchestrator settings: Ollama keys, Langfuse config,
GUI toggles, image-gen keys, etc.).

All defaults are empty strings / safe defaults — NO committed secrets. The
user sets them via env vars (uppercase, auto-mapped by pydantic-settings) or
a `.env` file in the working directory. See `bot/.env.example` for a
template.

Env-var overrides (uppercase of the field name):
  DISCORD_BOT_TOKEN / DISCORD_BOT_GUILD_ID / DISCORD_BOT_ALLOWED_CHANNEL_IDS
  / DISCORD_BOT_SESSION_CATEGORY_ID
  OPENCODE_SERVER_URL / OPENCODE_SERVER_PASSWORD / OPENCODE_SERVE_ENABLED
  / OPENCODE_SERVE_PORT / OPENCODE_SERVE_HOSTNAME / OPENCODE_SERVE_CORS
  / OPENCODE_SERVE_STARTUP_TIMEOUT / OPENCODE_SERVE_CWD
  VOICE_MESSAGE_ENABLED / VOICE_MESSAGE_TRIGGER_CHANNEL_ID
  / VOICE_SILENCE_TIMEOUT_SECONDS / VOICE_CHUNK_SECONDS / VOICE_STT_PROVIDER
  / VOICE_STT_MODEL / VOICE_LOCAL_WHISPER_MODEL / VOICE_TTS_ENABLED
  / VOICE_TTS_MODEL / VOICE_TTS_VOICE / VOICE_TTS_SPEED
  WHISPER_MODEL / WHISPER_DEVICE / WHISPER_COMPUTE_TYPE
  OPENAI_API_KEY
  OLLAMA_API_URL / OLLAMA_AUTH_KEY / SLUG_MODEL / SLUG_TIMEOUT_SECONDS
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotConfig(BaseSettings):
    """Bot-only settings, decoupled from `core.settings.ExecutionSettings`.

    Fields are grouped by subsystem: Discord, opencode-serve, voice,
    faster-whisper, OpenAI (TTS), slug LLM. All defaults are safe/empty so
    the bot starts without configuration (and exits cleanly with a clear
    message if a required value like `discord_bot_token` is missing — see
    `bot/__main__.py`).
    """

    model_config = SettingsConfigDict(env_prefix="", env_file=".env")

    # --- Discord ---
    discord_bot_token: str = ""
    discord_bot_guild_id: int = 0
    # Parsed as JSON by pydantic-settings v2, e.g.
    # DISCORD_BOT_ALLOWED_CHANNEL_IDS='[123,456]'.
    discord_bot_allowed_channel_ids: list[int] = Field(default_factory=list)
    # 0/unset = create session channels with no parent category.
    discord_bot_session_category_id: int = 0

    # --- opencode serve lifecycle ---
    opencode_server_url: str = "http://127.0.0.1:4096"
    opencode_server_password: str = ""
    opencode_serve_enabled: bool = True
    opencode_serve_port: int = 4096
    opencode_serve_hostname: str = "127.0.0.1"
    opencode_serve_cors: list[str] = Field(default_factory=list)
    opencode_serve_startup_timeout: float = 30.0
    # Working directory for the `opencode serve` subprocess. Empty = use
    # Path.cwd() at launch (the historical behavior). Set to an absolute path
    # to decouple the server's project dir from the bot's launch dir — useful
    # when the bot's .env lives in a subdirectory but the user's .opencode/
    # (plans, agents, config) lives at the project root. The server's
    # process.cwd() determines which .opencode/ tree every session resolves to
    # (opencode's defaultDirectory resolver falls back to process.cwd() when
    # no ?directory= query / x-opencode-directory header is on the request).
    opencode_serve_cwd: str = ""

    # --- voice messages (press-hold mic in the mobile composer) ---
    # When True, the bot transcribes Discord voice-message attachments arriving
    # as plain messages and routes the transcript to plan-author (follow-up in
    # a session channel, or new session in the trigger channel below). Set
    # False to disable voice-message intake without affecting /oc_talk or
    # /oc_voice.
    voice_message_enabled: bool = True
    # The channel id where voice messages trigger a NEW plan-author session
    # (branch c of on_message). Voice messages in existing session channels
    # still work as follow-ups regardless of this value. 0 = disabled (only
    # follow-ups work). Defaults to #new-plans (1533242090862149842).
    voice_message_trigger_channel_id: int = 1533242090862149842

    # --- voice (STT/TTS) ---
    voice_silence_timeout_seconds: float = 10.0
    voice_chunk_seconds: float = 5.0
    # "openai" = cloud Whisper API (needs OPENAI_API_KEY); "local" = in-process
    # faster-whisper (CTranslate2, needs `pip install faster-whisper`); "auto" =
    # try local first, fall back to cloud on any error.
    voice_stt_provider: str = "local"
    # Cloud Whisper model name (applies only when voice_stt_provider is "openai"
    # or the "auto" fallback to cloud).
    voice_stt_model: str = "whisper-1"
    # Local Whisper model size for faster-whisper (tiny/base/small/medium/large).
    voice_local_whisper_model: str = "medium"
    # TTS (response playback): when True, the bot synthesizes the final
    # assistant text via OpenAI TTS and plays it in the voice channel.
    voice_tts_enabled: bool = True
    voice_tts_model: str = "tts-1"
    voice_tts_voice: str = "alloy"
    voice_tts_speed: float = 1.0

    # --- faster-whisper (local STT, CTranslate2) ---
    # faster-whisper model name (applies only when voice_stt_provider is
    # "local" or the "auto" local path). One of tiny/base/small/medium/large
    # (or a CTranslate2-compatible path). `base` (~140MB) is CPU-feasible.
    whisper_model: str = "medium"
    # "cpu" or "cuda" — faster-whisper device.
    whisper_device: str = "cpu"
    # CTranslate2 compute type: "int8" (CPU), "float16" (GPU), "int8_float16"
    # (GPU mixed), etc. `int8` is the CPU default.
    whisper_compute_type: str = "int8"

    # --- OpenAI (TTS only — STT cloud path also uses this key) ---
    # Needed for TTS (voice_tts_enabled=True) and for the cloud STT fallback
    # (voice_stt_provider="openai" or "auto"). Empty = those features raise a
    # clear error at call time; the bot still starts.
    openai_api_key: str = ""

    # --- slug LLM (channel-name generation via Ollama Cloud or any
    # OpenAI-compatible /chat/completions endpoint) ---
    ollama_api_url: str = "https://ollama.com/v1"
    ollama_auth_key: str = ""
    slug_model: str = "gpt-oss:20b-cloud"
    slug_timeout_seconds: float = 8.0


# Module-level singleton. Importers read `config.<field>` (NOT a fresh
# `BotConfig()`), so CLI overrides in `bot/__main__.py` that mutate the
# singleton propagate to every module. The bot process constructs ONE
# `BotConfig` (here at import) and shares it everywhere.
config = BotConfig()
