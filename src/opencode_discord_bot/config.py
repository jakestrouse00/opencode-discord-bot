"""Standalone settings for the opencode Discord control bot.

`BotConfig` holds ONLY the fields the bot package (`opencode_discord_bot/`)
reads, decoupled from `core.settings.ExecutionSettings` so the bot can be
imported, installed, and run without dragging the orchestrator's `core/`
package along (which carries 20+ unrelated orchestrator settings: Ollama
keys, Langfuse config, GUI toggles, image-gen keys, etc.).

All defaults are empty strings / safe defaults — NO committed secrets. The
user sets them via env vars (uppercase, auto-mapped by pydantic-settings) or
a `.env` file in the working directory. See `.env.example` (at the repo
root) for a template.

Env-var overrides (uppercase of the field name):
  DISCORD_BOT_TOKEN / DISCORD_BOT_GUILD_ID / DISCORD_BOT_ALLOWED_CHANNEL_IDS
  / DISCORD_BOT_SESSION_CATEGORY_ID
  OPENCODE_SERVER_URL / OPENCODE_SERVER_PASSWORD / OPENCODE_SERVER_USERNAME
  / OPENCODE_SERVE_ENABLED / OPENCODE_SERVE_PORT / OPENCODE_SERVE_HOSTNAME
  / OPENCODE_SERVE_CORS / OPENCODE_SERVE_STARTUP_TIMEOUT / OPENCODE_SERVE_CWD
  / OC_SINGLETON_LOCK
  OPENCODE_DEFAULT_MODEL / OPENCODE_ASSISTANT_MODEL
  VOICE_MESSAGE_ENABLED / VOICE_MESSAGE_TRIGGER_CHANNEL_ID
  / VOICE_SILENCE_TIMEOUT_SECONDS / VOICE_CHUNK_SECONDS / VOICE_STT_PROVIDER
  / VOICE_STT_MODEL / VOICE_LOCAL_WHISPER_MODEL / VOICE_TTS_ENABLED
  / VOICE_TTS_MODEL / VOICE_TTS_VOICE / VOICE_TTS_SPEED
  WHISPER_MODEL / WHISPER_DEVICE / WHISPER_COMPUTE_TYPE
  SPEAKERS_DIR / SPEAKER_ID_ENABLED / SPEAKER_MATCH_THRESHOLD
  OPENAI_API_KEY
  OLLAMA_API_URL / OLLAMA_AUTH_KEY / SLUG_MODEL / SLUG_TIMEOUT_SECONDS
  COMULYTIC_ENABLED / COMULYTIC_JWT / COMULYTIC_REFRESH_TOKEN
  / COMULYTIC_API_BASE / COMULYTIC_WEB_BASE / COMULYTIC_AUDIO_PATH
  / COMULYTIC_POLL_INTERVAL_SECONDS / COMULYTIC_POLL_PAGE_SIZE
  / COMULYTIC_USER_AGENT / COMULYTIC_PLAN_TYPE / COMULYTIC_RELOGIN_WARN_DAYS
  / COMULYTIC_STATE_FILE / COMULYTIC_DISCORD_POINTER_CHANNEL_ID
  / COMULYTIC_QUESTION_TIMEOUT_SECONDS / COMULYTIC_QUESTION_POLL_INTERVAL_SECONDS
  / COMULYTIC_MAX_DURATION_HOURS / COMULYTIC_MAX_DURATION_MINUTES
  / COMULYTIC_MAX_DURATION_SECONDS
  / DASHBOARD_ENABLED / DASHBOARD_PORT / DASHBOARD_TOKEN
  / MONITOR_ENABLED / MONITOR_CHANNEL_ID / MONITOR_USER_ID
  / MONITOR_POLL_INTERVAL_SECONDS
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
    `opencode_discord_bot/__main__.py`).
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
    # Default port 4097 (NOT 4096 — 4096 is now the `opencode-remote-gui`
    # Flet UI port, so http://127.0.0.1:4096 shows the GUI instead of the
    # opencode serve basic-auth dialog). The spawned `opencode serve`
    # subprocess listens on 4097; the GUI's backend talks to it on this URL.
    # Mirrors `opencode-rest-client`'s `config.py` defaults. If a stale `.env`
    # pins 4096, update it to 4097 (the env override wins over this default).
    opencode_server_url: str = "http://127.0.0.1:4097"
    opencode_server_password: str = ""
    # Basic-auth username sent to the opencode server (the server always
    # expects "opencode"; exposed for non-default server configs and to keep
    # the client + .env in sync rather than hardcoding a magic string in
    # `opencode_client._auth`). Read by `OpencodeClient._auth()` when
    # `opencode_server_password` is set.
    opencode_server_username: str = "opencode"
    opencode_serve_enabled: bool = True
    # The port the spawned `opencode serve` listens on. Default 4097 (the
    # `opencode-remote-gui` backend takes 4096 so the user opens
    # http://127.0.0.1:4096 in the browser and sees the GUI, not the
    # opencode serve API). Keep in sync with `opencode_server_url` above.
    opencode_serve_port: int = 4097
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

    # --- singleton lock (single-instance enforcement) ---
    # Absolute path to the OS file lock that prevents a second bot process
    # from racing the Discord gateway. Empty = `.opencode-discord-bot.lock`
    # in the launch cwd. Override for non-standard launch dirs (e.g. when
    # multiple bots share a cwd but must not race each other). Read by
    # `SingletonLock.acquire_or_raise`.
    oc_singleton_lock: str = ""

    # --- model overrides (optional) ---
    # When non-empty, the bot sends this model in the prompt body for every
    # opencode prompt it dispatches, overriding the per-agent frontmatter
    # `model:` field on the opencode server side. Empty = each opencode agent's
    # own frontmatter model wins (the historical behavior). Two separate fields
    # so the default agent (`/oc` + plain-text follow-ups, `agent=None`) and
    # the oc-assistant agent (`/oc_plan`, `/oc_voice`, `/oc_talk`, voice-message
    # trigger, Comulytic bridge) can target different models independently.
    # Model ids are provider-scoped — e.g. `ollama-cloud/glm-5.2`,
    # `anthropic/claude-sonnet-4`, `openai/gpt-5`. See the opencode server docs
    # for the accepted model id format on your provider.
    opencode_default_model: str = ""
    opencode_assistant_model: str = ""

    # --- voice messages (press-hold mic in the mobile composer) ---
    # When True, the bot transcribes Discord voice-message attachments arriving
    # as plain messages and routes the transcript to oc-assistant (follow-up in
    # a session channel, or new session in the trigger channel below). Set
    # False to disable voice-message intake without affecting /oc_talk or
    # /oc_voice.
    voice_message_enabled: bool = True
    # The channel id where voice messages trigger a NEW oc-assistant session
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

    # --- STT domain-word biasing (optional, all default empty = no change) ---
    # Space-separated domain words whose generation probability is boosted by
    # the faster-whisper CTranslate2 decoder (local + auto paths only; the
    # cloud OpenAI Whisper API has no equivalent). Purpose-built for rare /
    # out-of-vocabulary words the model mishears (e.g. "comulytic" →
    # "Conulec" / "Kamilik"). Empty = no bias (current behavior).
    voice_stt_hotwords: str = ""
    # Short context sentence passed as decoder prefix context. Used by BOTH
    # the local path (faster-whisper `initial_prompt=`) and the cloud path
    # (OpenAI `prompt=`, up to 224 tokens). Best as a natural sentence
    # containing the domain words, e.g. "The user discusses Comulytic,
    # opencode, and Pycord." Empty = no prompt (current behavior).
    voice_stt_prompt: str = ""
    # JSON object mapping known mishearings to corrections, applied as a
    # case-insensitive whole-word replace on the final transcript (both
    # local and cloud paths). 100% reliable for enumerated mishearings; runs
    # once on the final string (no inference cost). e.g.
    # '{"Conulec":"comulytic","Kamilik":"comulytic"}'. Malformed JSON = logged
    # WARNING + no-op (STT never blocks on a bad config). Empty = no-op.
    voice_stt_replacements: str = ""

    # --- speaker identification (diarization, optional) ---
    # Speaker ID adds per-speaker labels to multi-speaker recordings
    # (meetings, not just solo notes) so the oc-assistant agent can produce
    # coherent meeting notes. Layered on top of `transcribe_audio` (the
    # anonymous STT primitive stays unchanged); requires the optional
    # `speakers` extra (`pip install 'opencode-discord-bot[speakers]'`) and
    # a `Speakers/<name>/` folder of reference audio. Degrades gracefully:
    # when `speaker_id_enabled` is False, OR pyannote.audio isn't installed,
    # OR `Speakers/` is absent/empty, `transcribe_with_speakers` falls back
    # to the existing anonymous single-string transcript — no error.
    # Directory holding reference audio samples per speaker. Each subfolder
    # is one speaker (the folder name is the default speaker label); each
    # audio file in it (`.wav`/`.mp3`/`.m4a`/`.ogg`/`.flac`) is a reference
    # sample. A relative path resolves against the bot's launch cwd (same
    # convention as `comulytic_state_file`).
    speakers_dir: str = "Speakers"
    # Master toggle. When False, skip diarization entirely and return the
    # anonymous transcript. When True but pyannote isn't installed / `Speakers/`
    # is empty, log a warning and fall back to anonymous.
    speaker_id_enabled: bool = True
    # Cosine-similarity cutoff for matching a turn's embedding to a known
    # speaker's reference embeddings. A turn is attributed to the known
    # speaker with the max cosine similarity ONLY if that max exceeds this
    # threshold; otherwise the turn gets a generic `Speaker N` label.
    speaker_match_threshold: float = 0.75

    # HuggingFace access token — read by speakers.py for pyannote.audio
    # pretrained-model downloads (pyannote/speaker-diarization-3.1 +
    # wespeaker-voxceleb-resnet34-LM are gated models). Empty = pyannote
    # will 401 on first model load; speaker ID then raises + the
    # transcribe_with_speakers orchestrator falls back to anonymous STT.
    # Get one at https://huggingface.co/settings/tokens (read scope).
    hf_token: str = ""

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

    # --- Comulytic cloud bridge (polling-based, optional) ---
    # MASTER ENABLE for the Comulytic bridge. Default False = the bridge
    # process (`python -m opencode_discord_bot.bridge` or the
    # `comulytic-bridge` console script) refuses to start, so the feature is
    # fully OFF by default. Flip to True via the `.env` file or env var:
    #   COMULYTIC_ENABLED=true
    # to activate polling + oc-assistant routing. When False, the bridge
    # exits immediately at startup with a clear message (see `bridge.main`).
    # Even when this is True, the bridge still requires `comulytic_jwt` to
    # be set — if the JWT is empty it exits with a different clear message.
    comulytic_enabled: bool = False
    # Bearer JWT captured from a real login at web.comulytic.ai.
    # HS256, 150-day TTL. AEIYDDL confirmed the login exchange:
    #   POST /api/kirby/v1/auth/social/login (Apple) or
    #   POST /api/kirby/v1/auth/login/email (email+password, scriptable, preferred
    #   for automation) returns {data:{accessToken, refreshToken, tokenType:"bearer",
    #   expiresIn:12960000, user:{...}}}. The 365-day refreshToken EXISTS but the
    #   refresh *call* (endpoint path/body) is a capture gap — until re-captured,
    #   re-login via /auth/login/email before exp. See SETUP_GUIDE.md "Comulytic
    #   bridge" section for capture instructions.
    comulytic_jwt: str = ""
    # The 365-day refreshToken returned alongside the access JWT by social/login
    # (or login/email). Persisted for future use once the refresh endpoint call is
    # captured. Empty = not captured; the bridge treats the access JWT as
    # non-refreshable and warns before exp instead.
    comulytic_refresh_token: str = ""
    # Base URL of the Comulytic cloud API (Bearer JWT host).
    comulytic_api_base: str = "https://api.comulytic.ai"
    # Base URL of the web app (audio proxy host for Path B). The audio-range
    # proxy serves the complete MP3 via a stable noteId-based URL with no embedded
    # expiry; only the Bearer JWT cookie (~150 days) rotates.
    comulytic_web_base: str = "https://web.comulytic.ai"
    # Audio download path priority: "proxy" (Path B — cookie-auth proxy at
    # web.comulytic.ai/api/note/audio-range/{noteId}, RECOMMENDED — no per-URL
    # expiry) | "presigned" (Path A — pre-signed S3 URL via noteDetail, 48h TTL,
    # re-mint each cycle). Default "proxy" per the consolidated report's
    # synthesis (AEIYDDL's strongest recommendation). Path A is the fallback.
    comulytic_audio_path: str = "proxy"
    # How often (seconds) to probe /note/paging for new recordings. Cheap
    # probe (pageSize:1) returns total + newest noteId; full enumerate only
    # when total changes. Comulytic's web client polls ~15s; 60s is
    # conservative for an autonomous bridge (fewer API calls, less WAF
    # attention). Lower to 15-30s if latency matters.
    comulytic_poll_interval_seconds: float = 60.0
    # Page size for full enumeration when total changes (Comulytic default 20).
    comulytic_poll_page_size: int = 20
    # Value of the x-device-model AND User-Agent headers. Empty = the bridge
    # derives a stable desktop Chrome UA at startup. Must match the UA used
    # during JWT capture to minimize fingerprint mismatch.
    comulytic_user_agent: str = ""
    # Directive sent to oc-assistant: "" (let oc-assistant classify) |
    # "actionable" | "note". Mirrors the [PLAN_TYPE_PRESELECTED] directive
    # from opencode_discord_bot/commands.py.
    comulytic_plan_type: str = ""
    # Warn N days before JWT exp (logged on startup + each poll cycle if
    # close). The consolidated report recommends proactive refresh at exp-24h
    # once the refresh endpoint is confirmed; until then, re-login via
    # /auth/login/email ~1-7 days before exp (the refresh *call* is a gap).
    comulytic_relogin_warn_days: float = 1.0
    # Path to the seen-set state file (noteIds already processed). Relative
    # paths resolve against the bridge's cwd. Gitignored — runtime state.
    comulytic_state_file: str = ".comulytic-seen.json"

    # --- Comulytic bridge -> Discord channel (mirrors /oc_talk) ---
    # When non-zero, the bridge posts a "Created #channel" pointer to this
    # channel id when it routes a new recording to oc-assistant (mirrors
    # /oc_talk's ctx.followup.send pointer). 0 = no pointer; the created
    # channel is the sole discoverability surface. Useful when you want a
    # single "firehose" channel to watch for new bridge activity.
    comulytic_discord_pointer_channel_id: int = 0
    # How long (seconds) to wait for the user's plain-text reply to a
    # oc-assistant clarifying question in the bridge's Discord channel before
    # rejecting it (unblocking the agent). The main bot uses buttons that
    # wait indefinitely; plain-text polling needs a ceiling. 300s = 5 min.
    comulytic_question_timeout_seconds: float = 300.0
    # How often (seconds) to poll GET /question + GET /permission while the
    # oc-assistant session is running. Matches the main bot's 2.0s cadence.
    comulytic_question_poll_interval_seconds: float = 2.0

    # --- Comulytic bridge: max-recording-duration cap ---
    # Recordings longer than (hours + minutes + seconds) are marked as seen
    # but NOT transcribed — skips both WAV conversion and the expensive local
    # Whisper STT pass. Guards against accidental recordings (e.g. a recorder
    # left on for hours) being processed. The recording is still downloaded
    # (cheap relative to Whisper) so its duration can be probed via ffprobe;
    # if the probe fails the recording is transcribed as before (fail-open).
    # Set all three to 0 to disable the cap entirely. Default 1h0m0s = 3600s.
    comulytic_max_duration_hours: int = 1
    comulytic_max_duration_minutes: int = 0
    comulytic_max_duration_seconds: int = 0

    # --- Ops dashboard ---
    # Master switch for the token-gated HTTP dashboard (stats + controls)
    # served in-process alongside the bot. Disabled by default; requires a
    # non-empty dashboard_token to start (an empty token keeps the server
    # from EVER starting, so local default behavior is unchanged).
    dashboard_enabled: bool = False
    # Port the dashboard binds (0.0.0.0 inside the container; Fly's
    # [[services]] block maps 443 -> this internal port).
    dashboard_port: int = 8080
    # Shared-secret bearer token. Every dashboard request (page + API) must
    # present it via `Authorization: Bearer <token>` or `?token=<t>`.
    # NO committed default — set via env var / Fly secret.
    dashboard_token: str = ""

    # --- Session monitor (read-only desktop-session notifications) ---
    # Master switch for the background monitor that watches the opencode
    # server (typically the user's DESKTOP `opencode serve`, reached via the
    # Fly/Tailscale setup) for pending permission requests, pending
    # questions, and session completions, and posts a Discord embed per
    # event to `monitor_channel_id`. READ-ONLY — the monitor never
    # replies/rejects/aborts anything; approvals stay at the desktop. The
    # monitor excludes sessions bound to Discord channels in either
    # SessionRouter file (those already post their responses in their own
    # channels — no duplicate notifications). Default False = the monitor
    # never starts; flip via `MONITOR_ENABLED=true`.
    monitor_enabled: bool = False
    # The Discord text channel the monitor posts event embeds to. Defaults
    # to the user's #opencode-monitor channel (a non-secret guild id). 0
    # = monitor disabled even when monitor_enabled is true.
    monitor_channel_id: int = 1544715093491847249
    # The Discord user id @mentioned in each monitor embed's message content
    # so the phone actually buzzes (a plain embed doesn't notify). 0 = no
    # mention prefix. Non-secret; get it via Discord Developer Mode ->
    # right-click your user -> Copy User ID.
    monitor_user_id: int = 0
    # How often (seconds) the monitor polls GET /session/status + /question
    # + /permission. 10s is responsive without hammering the server (3
    # cheap GETs per cycle). Read per-cycle so live tweaks apply.
    monitor_poll_interval_seconds: float = 10.0


# Module-level singleton. Importers read `config.<field>` (NOT a fresh
# `BotConfig()`), so CLI overrides in `opencode_discord_bot/__main__.py` that
# mutate the singleton propagate to every module. The bot process constructs
# ONE `BotConfig` (here at import) and shares it everywhere.
config = BotConfig()


def reload_config() -> None:
    """Re-read `.env` + env vars and mutate the `config` singleton in place.

    Used by the `/oc_setup` slash command after it writes new guild IDs
    (category id, channel ids, guild id) to `.env` via `env_writer`. Every
    module that holds a reference to `config` (imported as
    `from opencode_discord_bot.config import config`) sees the new values
    on its next read — no re-import needed.

    Constructs a fresh `BotConfig()` (which re-reads `.env` + env vars at
    construction per `model_config = SettingsConfigDict(env_file=".env")`)
    and copies its fields onto the existing singleton via `__dict__.update`,
    so the singleton's identity is preserved (reference holders see the
    update). Env-var overrides still win over `.env` values, matching the
    normal `pydantic-settings` precedence.
    """
    fresh = BotConfig()
    config.__dict__.update(fresh.model_dump())
