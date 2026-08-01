"""Voice-channel support for the Discord bot (`/oc_voice` command).

Wraps Pycord's recording/sinks API (`VoiceClient.start_recording` /
`stop_recording` / `discord.sinks.Sink`) to capture the user's spoken
plan/note, transcribe it via OpenAI Whisper (cloud) or local
faster-whisper (CTranslate2, opt-in), and synthesize the bot's response via
OpenAI TTS for playback in the voice channel.

.. warning:: Pycord's recording API emits a `RuntimeWarning` on every
   `start_recording`/`stop_recording` call noting that "Voice reception is
   currently broken due to Discord's DAVE (End-to-End Encryption) protocol"
   (see Pycord issue #3139). The API exists and is wired here as the plan
   specifies, but audio capture does not currently function on DAVE-enabled
   voice channels (all modern Discord voice) — a previous approach of
   patching `.venv` site-packages was abandoned as it did not work, and a
   replacement voice-capture solution is being sought. The STT/TTS/
   session-routing parts of the feature are unaffected by the DAVE issue.

The module is lazy about heavy imports: `openai` is constructed inside the
transcribe/synthesize functions (so the bot starts fine with an empty
`openai_api_key`), and `faster_whisper` (the local model) is imported only inside
`_get_local_whisper()` (so the bot starts fine without `faster-whisper`
installed). Both raise clear errors at call time if the required key/package
is missing.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
import warnings
from typing import TYPE_CHECKING, Callable, Optional

import discord
from discord.sinks import WaveSink

from opencode_discord_bot.config import config

if TYPE_CHECKING:
    from opencode_discord_bot.commands import OpencodeBot

_log = logging.getLogger("bot.voice")

# Module-level singleton for the local Whisper model — the multi-second load
# happens once per process. None until first use.
_LOCAL_WHISPER_MODEL = None


class SilenceDetectSink(WaveSink):
    """A WaveSink that tracks the last audio-packet timestamp for silence detection.

    Inherits WAV formatting from `WaveSink` (so `get_all_audio()` returns
    WAV-formatted BytesIO buffers ready for the Whisper API). Overrides
    `write()` to stamp `self.last_audio_time = time.monotonic()` on every
    audio packet; `VoiceSession._silence_monitor` polls this timestamp.

    .. note:: Pycord 2.8's `SinkEventRouter._register_listeners` accesses
       `sink.__sink_listeners__` unconditionally, but the `discord.sinks`
       module never defines it on the base `Sink`/`Filters` classes — a
       library bug. We set it to an empty list here so the router's
       `for name, method_name in sink.__sink_listeners__`
       loop iterates zero times instead of raising `AttributeError`.
    """

    # Pycord 2.8.1 library bug workaround: the receive router expects this
    # attribute but the sinks module never sets it. Empty list = no events.
    __sink_listeners__: list = []

    def __init__(self, *, filters=None) -> None:
        super().__init__(filters=filters)
        self.last_audio_time: float = time.monotonic()

    def walk_children(self, **kwargs):
        # Pycord 2.8.1 library bug workaround: SinkEventRouter.register_events()
        # calls self.sink.walk_children() but Sink doesn't define it (the
        # sinks module was never updated to match the receive router).
        # Return empty so the router iterates zero children instead of raising
        # AttributeError — same class of bug as __sink_listeners__ above.
        return []

    @discord.sinks.Filters.container
    def write(self, data, user) -> None:  # noqa: D401 — matches Sink.write signature
        self.last_audio_time = time.monotonic()
        super().write(data, user)


def _openai_client():
    """Construct a lazy AsyncOpenAI client (deferred so the bot starts without a key)."""
    from openai import AsyncOpenAI

    if not config.openai_api_key:
        raise RuntimeError(
            "openai_api_key is empty. Set OPENAI_API_KEY (or fill in "
            "openai_api_key in bot/config.py / .env) to use the cloud STT/TTS path."
        )
    return AsyncOpenAI(api_key=config.openai_api_key)


def _get_local_whisper():
    """Lazy singleton for the in-process faster-whisper model (local STT).

    `import faster_whisper` is lazy so the bot starts fine without
    `faster-whisper` installed. The model load (~140MB for `base`) happens
    once per process and is cached on the module-level
    `_LOCAL_WHISPER_MODEL`. Raises a clear RuntimeError if `faster-whisper`
    isn't installed.

    Uses CTranslate2 under the hood (the `faster-whisper` backend), which is
    4x faster / 2x smaller than openai-whisper with equivalent accuracy. No
    cloud API, no per-request cost, privacy-preserving.
    """
    global _LOCAL_WHISPER_MODEL
    if _LOCAL_WHISPER_MODEL is not None:
        return _LOCAL_WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "local Whisper not available — install faster-whisper "
            "(pip install faster-whisper) or set VOICE_STT_PROVIDER=openai "
            "for the cloud path."
        ) from e
    _LOCAL_WHISPER_MODEL = WhisperModel(
        config.voice_local_whisper_model,
        device=config.whisper_device,
        compute_type=config.whisper_compute_type,
    )
    return _LOCAL_WHISPER_MODEL


async def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe WAV bytes to text, dispatching on `voice_stt_provider`.

    Provider modes:
      - ``"openai"``: cloud Whisper API (`client.audio.transcriptions.create`).
      - ``"local"``: in-process `faster-whisper` model (run in a thread to
        avoid blocking the event loop; needs a temp WAV file on disk).
      - ``"auto"``: try local first, fall back to cloud on any error.

    Returns the transcribed text (empty string if transcription yields nothing).
    """
    provider = config.voice_stt_provider
    if provider == "local":
        return await _transcribe_local(audio_bytes)
    if provider == "openai":
        return await _transcribe_cloud(audio_bytes)
    # auto
    try:
        return await _transcribe_local(audio_bytes)
    except Exception as e:  # noqa: BLE001 — auto falls back on any error
        _log.warning("local STT failed (%r); falling back to cloud", e)
        return await _transcribe_cloud(audio_bytes)


async def _transcribe_cloud(audio_bytes: bytes) -> str:
    """Cloud Whisper API transcription."""
    client = _openai_client()
    buf = io.BytesIO(audio_bytes)
    buf.name = "audio.wav"
    resp = await client.audio.transcriptions.create(
        model=config.voice_stt_model, file=buf
    )
    return (getattr(resp, "text", "") or "").strip()


async def _transcribe_local(audio_bytes: bytes) -> str:
    """Local in-process faster-whisper transcription (run in a thread to avoid blocking).

    faster-whisper's `model.transcribe()` returns `(segments, info)` where
    `segments` is an iterable of objects with `.text` attributes. We join them
    with spaces. Like openai-whisper, it reads from a file path, not bytes —
    write a temp WAV first.
    """
    model = _get_local_whisper()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        segments, _info = await asyncio.to_thread(
            model.transcribe, tmp_path, language="en"
        )
        # faster-whisper's segments is a generator — materialize + join.
        return " ".join(seg.text for seg in segments).strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def synthesize_speech(text: str) -> bytes:
    """Synthesize text to MP3 bytes via OpenAI TTS (cloud-only).

    Raises RuntimeError if `openai_api_key` is empty. Returns MP3 bytes ready
    for `FFmpegPCMAudio` playback (ffmpeg converts MP3 → PCM on the fly).
    """
    client = _openai_client()
    resp = await client.audio.speech.create(
        model=config.voice_tts_model,
        voice=config.voice_tts_voice,
        input=text,
        response_format="mp3",
        speed=config.voice_tts_speed,
    )
    return resp.content


# Media types we accept for /oc_talk attachments. Whisper transcribes audio,
# so for video files we extract the audio track via ffmpeg first. We accept
# any audio/* or video/* MIME type plus a few common extensions that Discord
# sometimes labels application/ogg (voice messages). Cloud Whisper accepts
# flac, mp3, mp4, mpeg, mpga, oga, ogg, wav, webm directly; local whisper
# accepts anything ffmpeg can decode. For video, ffmpeg extracts the audio
# stream and discards the video.
_ACCEPTED_EXTENSIONS = {
    # Audio
    "wav",
    "mp3",
    "m4a",
    "ogg",
    "oga",
    "opus",
    "webm",
    "flac",
    "mpeg",
    "mpga",
    "aac",
    "wma",
    # Video (audio track extracted via ffmpeg)
    "mp4",
    "mov",
    "m4v",
    "avi",
    "mkv",
    "flv",
    "wmv",
    "mpg",
    "ts",
    "3gp",
    "ogv",
}
_ACCEPTED_CONTENT_PREFIXES = ("audio/", "video/", "application/ogg")


def is_transcribable_attachment(attachment) -> bool:
    """Return True if a discord.Attachment is audio or video we can transcribe.

    Accepts any ``audio/*`` or ``video/*`` content type, plus ``application/ogg``
    (Discord voice messages), with a filename-extension fallback. Video files
    are accepted because the audio track is extracted via ffmpeg before
    transcription — only the audio is sent to Whisper.
    """
    ct = (attachment.content_type or "").lower()
    if ct.startswith(_ACCEPTED_CONTENT_PREFIXES):
        return True
    name = (attachment.filename or "").lower()
    if "." in name:
        ext = name.rsplit(".", 1)[-1]
        if ext in _ACCEPTED_EXTENSIONS:
            return True
    return False


async def extract_audio_to_wav(
    media_bytes: bytes,
    *,
    content_type: str | None = None,
    filename: str | None = None,
) -> bytes:
    """Convert uploaded audio/video bytes to WAV bytes for the Whisper transcriber.

    Accepts audio files (mp3, m4a, ogg, …) and video files (mov, mp4, avi, …).
    For video input, ffmpeg extracts the audio track and discards the video
    stream (``-vn``) — only the audio reaches Whisper.

    ``transcribe_audio`` ultimately feeds bytes to Whisper (cloud or local).
    Cloud Whisper accepts many formats directly, but the local faster-whisper
    path expects WAV-formatted input (it writes the bytes to a temp ``.wav``
    file at `bot/voice.py:_transcribe_local`). To keep both paths working with
    a single entry point, we always normalize to WAV unless the input is
    already WAV.

    Uses ffmpeg (already on PATH as a `discord.FFmpegPCMAudio` dependency) via
    an async subprocess: ``ffmpeg -i - -vn -f wav -ac 1 -ar 16000 -`` reads from
    stdin, writes mono 16kHz WAV bytes to stdout. Raises RuntimeError if ffmpeg
    is missing or fails.
    """
    # Detect WAV input (by content_type or .wav extension) and pass through.
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    is_wav = (
        ct.startswith("audio/wav")
        or ct.startswith("audio/wavx")
        or ct.startswith("audio/x-wav")
        or (name.endswith(".wav"))
    )
    if is_wav:
        return media_bytes

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",  # drop video stream (audio-only output for video input)
        "-f",
        "wav",
        "-ac",
        "1",  # mono: Whisper expects mono
        "-ar",
        "16000",  # 16kHz: Whisper's native sample rate
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=media_bytes)
    if proc.returncode != 0 or not stdout:
        err = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"ffmpeg conversion to WAV failed (code {proc.returncode}): {err}"
        )
    return stdout


class VoiceSession:
    """Orchestrates the full voice interaction lifecycle for `/oc_voice`.

    Constructor takes the bot, the connected voice client, the text channel
    bound to the opencode session, the session id, the mode ("change"/"note"),
    and a finalize callback (usually `OpencodeBot._finalize_voice_session`).

    Lifecycle:
      - `start()`: begins chunked recording, launches the silence monitor
        and chunk loop tasks.
      - Three stop triggers, any of which finalizes: (1) "Stop Conversation"
        detected in a chunk transcription, (2) `/oc_voice_stop` (calls
        `stop()` directly), (3) `voice_silence_timeout_seconds` of continuous
        silence. The auto triggers call `_on_auto_stop()` which calls
        `stop()` + the finalize callback.
      - `stop()`: cancels tasks, stops recording, flushes the last chunk,
        returns the accumulated transcript.
      - `speak(text)`: TTS-synthesizes + plays text in the voice channel.
      - `handle_question(text)`: speaks a question, records a short reply,
        transcribes, returns the answer (for voice-driven follow-ups).
    """

    def __init__(
        self,
        bot: OpencodeBot,
        voice_client: discord.voice.VoiceClient,
        text_channel: discord.TextChannel,
        session_id: str,
        mode: str,
        finalize_callback: Callable[
            [Optional[object], VoiceSession, str], Awaitable[None]
        ],  # noqa: F821
    ) -> None:
        self.bot = bot
        self.voice_client = voice_client
        self.text_channel = text_channel
        self.session_id = session_id
        self.mode = mode
        self._finalize_callback = finalize_callback
        self._transcript: list[str] = []
        self._stop_event = asyncio.Event()
        self._finalized = False
        self._silence_task: asyncio.Task | None = None
        self._chunk_task: asyncio.Task | None = None
        self._current_sink: SilenceDetectSink | None = None
        self._recording = False

    async def start(self) -> None:
        """Begin recording + launch the silence monitor and chunk loop.

        Suppresses Pycord's hardcoded ``RuntimeWarning`` about DAVE breaking
        voice reception — the warning is noisy and unactionable in this repo
        (voice capture is broken pending a replacement solution; see the
        module docstring). The suppression keeps logs readable regardless of
        whether a working capture path is in place.
        """
        self._current_sink = SilenceDetectSink()
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Voice reception is currently broken",
                category=RuntimeWarning,
            )
            self.voice_client.start_recording(self._current_sink, self._on_chunk_done)
        self._recording = True
        self._silence_task = asyncio.create_task(self._silence_monitor())
        self._chunk_task = asyncio.create_task(self._chunk_loop())

    async def stop(self) -> str:
        """Stop recording + tasks, flush the last chunk, return the transcript.

        Returns the accumulated transcription (all chunk transcriptions joined
        with spaces). Safe to call multiple times (the `_finalized` guard
        prevents double-finalization via `_on_auto_stop`).
        """
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        if self._chunk_task and not self._chunk_task.done():
            self._chunk_task.cancel()
        # Flush the last chunk if still recording.
        if self._recording and self.voice_client.is_recording():
            try:
                self.voice_client.stop_recording()
            except Exception as e:  # noqa: BLE001
                _log.warning("stop_recording failed during stop(): %r", e)
            self._recording = False
            await self._transcribe_current_sink()
        return " ".join(self._transcript).strip()

    async def _chunk_loop(self) -> None:
        """Periodically stop + restart recording to get transcribable chunks.

        Every `voice_chunk_seconds`, stops recording (which fires
        `_on_chunk_done` → transcribes the chunk), then immediately starts a
        fresh recording with a new sink. This keeps each Whisper call small
        and lets the stop-phrase check happen near-real-time.
        """
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(config.voice_chunk_seconds)
                if self._stop_event.is_set():
                    break
                if self._recording and self.voice_client.is_recording():
                    try:
                        self.voice_client.stop_recording()
                    except Exception as e:  # noqa: BLE001
                        _log.warning("stop_recording in chunk loop failed: %r", e)
                    self._recording = False
                # `_on_chunk_done` fires the transcribe; restart recording
                # with a fresh sink after a short yield.
                await asyncio.sleep(0.1)
                if self._stop_event.is_set():
                    break
                self._current_sink = SilenceDetectSink()
                self.voice_client.start_recording(
                    self._current_sink, self._on_chunk_done
                )
                self._recording = True
        except asyncio.CancelledError:
            raise

    def _on_chunk_done(self, error: Exception | None) -> None:
        """Callback fired by Pycord after `stop_recording()` — transcribes the chunk.

        Runs the transcription asynchronously (the callback is sync, so we
        schedule the async transcribe via `asyncio.create_task`). Checks for
        the "stop conversation" phrase and triggers auto-stop if found.
        """
        if error is not None:
            _log.warning("recording chunk finished with error: %r", error)
        asyncio.create_task(self._transcribe_current_sink())

    async def _transcribe_current_sink(self) -> None:
        """Transcribe the current sink's audio and check for the stop phrase."""
        sink = self._current_sink
        if sink is None or not sink.audio_data:
            return
        try:
            sink.cleanup()
        except Exception as e:  # noqa: BLE001
            _log.warning("sink cleanup failed: %r", e)
        # Collect all users' audio mixed together (no diarization).
        wav_bytes = b""
        for user_id, audio_data in sink.audio_data.items():
            try:
                buf = audio_data.file
                buf.seek(0)
                wav_bytes += buf.read()
            except Exception as e:  # noqa: BLE001
                _log.warning("failed to read audio for user %s: %r", user_id, e)
        if not wav_bytes:
            return
        try:
            text = await transcribe_audio(wav_bytes)
        except Exception as e:  # noqa: BLE001
            _log.warning("transcribe failed: %r", e)
            return
        if text:
            self._transcript.append(text)
            _log.info("voice chunk transcribed: %d chars", len(text))
            # Check for the stop phrase (case-insensitive).
            if "stop conversation" in text.lower():
                _log.info("stop phrase detected — triggering auto-stop")
                self._stop_event.set()
                asyncio.create_task(self._on_auto_stop())

    async def _silence_monitor(self) -> None:
        """Poll the sink's last_audio_time; trigger auto-stop on silence timeout."""
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
                sink = self._current_sink
                if sink is None:
                    continue
                silence = time.monotonic() - sink.last_audio_time
                if silence >= config.voice_silence_timeout_seconds:
                    _log.info("silence timeout (%.1fs) — triggering auto-stop", silence)
                    self._stop_event.set()
                    asyncio.create_task(self._on_auto_stop())
                    return
        except asyncio.CancelledError:
            raise

    async def _on_auto_stop(self) -> None:
        """Handle automatic stop triggers (silence or stop phrase).

        Cancels the other task, stops recording, gets the transcript, and
        invokes the finalize callback. Guarded by `_finalized` so two
        near-simultaneous triggers don't double-finalize.
        """
        if self._finalized:
            return
        self._finalized = True
        transcript = await self.stop()
        try:
            await self._finalize_callback(None, self, transcript)
        except Exception as e:  # noqa: BLE001
            _log.warning("finalize callback raised: %r", e)

    async def speak(self, text: str) -> None:
        """TTS-synthesize `text` and play it in the voice channel (best-effort).

        Writes the MP3 bytes to a temp file, plays via `FFmpegPCMAudio`, and
        waits for playback to finish (using an `asyncio.Event` set in the
        `after` callback). Skips if the voice client isn't connected.
        """
        if not self.voice_client.is_connected():
            _log.warning("speak() called but voice client not connected")
            return
        try:
            mp3_bytes = await synthesize_speech(text)
        except Exception as e:  # noqa: BLE001
            _log.warning("TTS synthesize failed: %r", e)
            return
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(mp3_bytes)
            tmp_path = tmp.name
        try:
            done = asyncio.Event()

            def _after(err: Exception | None) -> None:
                done.set()

            source = discord.FFmpegPCMAudio(tmp_path)
            self.voice_client.play(source, after=_after)
            await asyncio.wait_for(done.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            _log.warning("TTS playback timed out")
        except Exception as e:  # noqa: BLE001
            _log.warning("TTS playback failed: %r", e)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def handle_question(self, question_text: str) -> str:
        """Speak a question, record a short spoken answer, transcribe + return it.

        Used by `poll_pending_requests` for voice-driven follow-up questions.
        Records for up to 30s (or the silence timeout), then transcribes and
        returns the answer text (empty string if nothing was captured).
        """
        await self.speak(question_text)
        # Record a short reply with the silence timeout.
        sink = SilenceDetectSink()
        self.voice_client.start_recording(sink, lambda err: None)
        self._recording = True
        try:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                silence = time.monotonic() - sink.last_audio_time
                # Stop after the user stops speaking for the silence timeout,
                # but give them a moment to start (don't fire immediately).
                if silence >= config.voice_silence_timeout_seconds and sink.audio_data:
                    break
        finally:
            if self.voice_client.is_recording():
                try:
                    self.voice_client.stop_recording()
                except Exception as e:  # noqa: BLE001
                    _log.warning("stop_recording in handle_question failed: %r", e)
            self._recording = False
        if not sink.audio_data:
            return ""
        try:
            sink.cleanup()
        except Exception:
            pass
        wav_bytes = b""
        for audio_data in sink.audio_data.values():
            buf = audio_data.file
            buf.seek(0)
            wav_bytes += buf.read()
        if not wav_bytes:
            return ""
        try:
            return await transcribe_audio(wav_bytes)
        except Exception as e:  # noqa: BLE001
            _log.warning("handle_question transcribe failed: %r", e)
            return ""


# Awaitable import for the finalize_callback type hint (avoid circular import).
from typing import (
    Awaitable,
)  # noqa: E402 — deferred to bottom to keep TYPE_CHECKING block clean
