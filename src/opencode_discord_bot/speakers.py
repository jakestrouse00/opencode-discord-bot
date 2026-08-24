"""Speaker identification (diarization) for multi-speaker recordings.

Layers speaker labels on top of the existing anonymous STT path
(``voice.transcribe_audio``). Optional — pulls in the heavy pyannote.audio +
torch stack via the ``speakers`` extra (``pip install
'opencode-discord-bot[speakers]'``). The bot starts fine WITHOUT the extra
installed; ``transcribe_with_speakers`` falls back to the anonymous transcript
when pyannote isn't importable, when ``Speakers/`` is absent/empty, or when
``config.speaker_id_enabled`` is False.

Reference speakers live in subfolders of ``config.speakers_dir`` (default
``Speakers/``, relative to the bot's launch cwd). Each subfolder is one
speaker; the folder name is the default speaker label. Audio files
(``.wav``/``.mp3``/``.m4a``/``.ogg``/``.flac``) inside are reference samples —
pyannote.audio reads them via torchaudio, so no ffmpeg pre-conversion is
needed. Unknown speakers (no reference match above
``config.speaker_match_threshold``) get generic ``Speaker N`` labels in
encounter order.

Transcript format sent to oc-assistant::

    Jake: hey let's ship the speaker ID feature.
    Speaker 1: sounds good, I'll review the PR.
    Jake: thanks.

HuggingFace token: pyannote.audio 3.x downloads pretrained models from
HuggingFace on first use, which requires accepting the model licenses + a
HF token. Set the ``HF_TOKEN`` env var before first run.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from opencode_discord_bot.config import config

if TYPE_CHECKING:
    import numpy as np  # noqa: F401

_log = logging.getLogger("bot.speakers")

# Module-level singletons for the pyannote models — the multi-second load
# happens once per process. None until first use. Mirrors voice.py's
# `_LOCAL_WHISPER_MODEL` pattern (voice.py:51).
_PYANNOTE_PIPELINE = None
_SPEAKER_EMBEDDING = None
# Cache of the reference-speaker embeddings dict. None = not loaded yet;
# {} = loaded but empty (Speakers/ absent or no audio). Restart to refresh
# (adding/removing reference audio requires a bot restart, same convention
# as the whisper model singleton).
_SPEAKERS_CACHE: dict[str, list] | None = None

# Audio extensions accepted as reference samples in `Speakers/<name>/`.
_REFERENCE_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}


def _bot_repo_root() -> Path:
    """Return the bot's own repo root (the dir containing ``.env`` / the
    ``Speakers/`` folder that ships with the repo), derived from this
    package's install location.

    ``__file__`` is ``.../src/opencode_discord_bot/speakers.py`` in a source
    install, or ``.../site-packages/opencode_discord_bot/speakers.py`` in a
    pip install. The repo root is two parents up in the source case
    (``src/opencode_discord_bot`` -> ``src`` -> repo root). In the
    site-packages case there is no ``Speakers/`` folder shipped, so this is
    only used as a FALLBACK when the cwd-relative path doesn't resolve —
    see ``_resolve_speakers_dir``.
    """
    here = Path(__file__).resolve()
    # src layout: <root>/src/opencode_discord_bot/speakers.py -> <root>
    return here.parent.parent.parent


def _resolve_speakers_dir(speakers_dir: str | Path) -> Path:
    """Resolve ``speakers_dir`` to an existing directory, or the best guess.

    Resolution order for a RELATIVE path:
      1. Against the process cwd (the documented behavior — matches
         ``BotConfig``'s cwd-relative ``.env`` convention).
      2. Against the bot's own repo root (derived from the package
         location) — fixes the common case where the bot is launched from a
         parent dir (e.g. a multi-project workspace root) so ``Speakers/``
         lives in the bot subdir, not the launch cwd.

    An ABSOLUTE ``speakers_dir`` is used as-is (only step 1 applies).

    Returns the first existing directory from the order above, or the
    cwd-relative path (step 1) if none exists (so the caller's "not found"
    warning reports the path the user configured, not a derived fallback).
    """
    p = Path(speakers_dir)
    if p.is_absolute():
        return p
    cwd_relative = Path.cwd() / p
    if cwd_relative.is_dir():
        return cwd_relative
    repo_relative = _bot_repo_root() / p
    if repo_relative.is_dir():
        return repo_relative
    return cwd_relative  # report the cwd-relative path (the configured one)


# ---------------------------------------------------------------------------
# Lazy singletons (mirror voice.py:150 _construct_local_whisper + voice.py:175
# _get_local_whisper + the module-level _LOCAL_WHISPER_MODEL at voice.py:51).
# ---------------------------------------------------------------------------


def _construct_pyannote_pipeline():
    """Synchronously construct the pyannote diarization pipeline (heavy).

    Import is lazy so the bot starts fine without the ``speakers`` extra
    installed. Raises a clear RuntimeError in that case. This function
    blocks for the multi-second model load and must NOT be called on the
    event loop — wrap the call site in ``asyncio.to_thread``.
    """
    if config.whisper_device == "cuda":
        # pyannote uses torch — same CUDA runtime as faster-whisper.
        # Register the nvidia-* wheel bin/ dirs so cuBLAS etc. load on GPU.
        from opencode_discord_bot.voice import _register_cuda_dll_dirs

        _register_cuda_dll_dirs()
    try:
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise RuntimeError(
            "speaker ID not available — install with "
            "`pip install 'opencode-discord-bot[speakers]'`"
        ) from e
    token = config.hf_token or None
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", token=token
    )
    if config.whisper_device == "cuda":
        try:
            import torch

            pipeline.to(torch.device("cuda"))
        except Exception:  # noqa: BLE001 — GPU is optional
            pass
    return pipeline


def _get_pyannote_pipeline():
    """Lazy singleton for the pyannote diarization pipeline."""
    global _PYANNOTE_PIPELINE
    if _PYANNOTE_PIPELINE is not None:
        return _PYANNOTE_PIPELINE
    _PYANNOTE_PIPELINE = _construct_pyannote_pipeline()
    return _PYANNOTE_PIPELINE


def _construct_speaker_embedding():
    """Synchronously construct the pyannote embedding model (heavy).

    Uses ``wespeaker-voxceleb-resnet34-LM`` (256-dim embeddings). The
    ``window="whole"`` setting makes ``__call__`` return a single embedding
    per file (not per-frame), which is what we want for short reference
    samples and per-turn slices.
    """
    if config.whisper_device == "cuda":
        from opencode_discord_bot.voice import _register_cuda_dll_dirs

        _register_cuda_dll_dirs()
    try:
        from pyannote.audio import Inference
        from pyannote.audio.core.model import Model
    except ImportError as e:
        raise RuntimeError(
            "speaker ID not available — install with "
            "`pip install 'opencode-discord-bot[speakers]'`"
        ) from e
    token = config.hf_token or None
    model = Model.from_pretrained(
        "pyannote/wespeaker-voxceleb-resnet34-LM",
        token=token,
    )
    return Inference(model, window="whole")


def _get_speaker_embedding():
    """Lazy singleton for the pyannote embedding model."""
    global _SPEAKER_EMBEDDING
    if _SPEAKER_EMBEDDING is not None:
        return _SPEAKER_EMBEDDING
    _SPEAKER_EMBEDDING = _construct_speaker_embedding()
    return _SPEAKER_EMBEDDING


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_speakers(
    speakers_dir: Path | str | None = None,
) -> dict[str, list]:
    """Load reference speaker embeddings from ``Speakers/<name>/`` subfolders.

    Returns ``{speaker_name: [embedding, embedding, ...]}`` where each
    embedding is a 1-D ``np.ndarray``. Returns ``{}`` when the directory
    doesn't exist or contains no audio (no error — speaker ID degrades to
    generic ``Speaker N`` labels). Lazily imports pyannote.audio inside
    (raises a clear RuntimeError when missing, caught by the caller).

    The result is cached at module level (``_SPEAKERS_CACHE``) so repeated
    calls within one process don't re-run the embedding model. Restart the
    bot to refresh after adding/removing reference audio.
    """
    global _SPEAKERS_CACHE
    if _SPEAKERS_CACHE is not None:
        return _SPEAKERS_CACHE

    if speakers_dir is None:
        speakers_dir = config.speakers_dir
    speakers_path = _resolve_speakers_dir(speakers_dir)
    if not speakers_path.is_dir():
        _log.warning(
            "Speakers dir not found — speaker ID disabled. Checked: %s "
            "(cwd=%s, SPEAKERS_DIR=%r). For a relative SPEAKERS_DIR the bot "
            "also looks under its own repo root (%s). Put `Speakers/<name>/` "
            "in one of these locations, or set SPEAKERS_DIR to an absolute path.",
            speakers_path,
            Path.cwd(),
            config.speakers_dir,
            _bot_repo_root(),
        )
        _SPEAKERS_CACHE = {}
        return _SPEAKERS_CACHE

    embedding_model = _get_speaker_embedding()

    result: dict[str, list] = {}
    for speaker_folder in sorted(speakers_path.iterdir()):
        if not speaker_folder.is_dir():
            continue
        audio_files = [
            f
            for f in sorted(speaker_folder.iterdir())
            if f.suffix.lower() in _REFERENCE_EXTENSIONS and f.is_file()
        ]
        if not audio_files:
            continue
        embeddings = []
        for audio_file in audio_files:
            try:
                emb = embedding_model(str(audio_file))
                import numpy as np

                arr = np.asarray(emb)
                if arr.ndim > 1:
                    arr = arr.mean(axis=0)
                embeddings.append(arr)
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "failed to compute embedding for %s: %r", audio_file, e
                )
        if embeddings:
            result[speaker_folder.name] = embeddings

    _SPEAKERS_CACHE = result
    return _SPEAKERS_CACHE


def _slice_wav(wav_bytes: bytes, start: float, end: float) -> bytes:
    """Slice WAV bytes to the ``[start, end]`` time range (seconds).

    Uses the stdlib ``wave`` module (no ffmpeg needed) to seek to the
    start frame, read the range, and re-wrap as a valid WAV file. Returns
    empty bytes if the range is outside the file.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        n_channels = wav.getnchannels()
        sampwidth = wav.getsampwidth()
        framerate = wav.getframerate()
        n_frames = wav.getnframes()
        start_frame = max(0, int(start * framerate))
        end_frame = min(int(end * framerate), n_frames)
        if end_frame <= start_frame:
            return b""
        wav.setpos(start_frame)
        frames = wav.readframes(end_frame - start_frame)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(frames)
    return out.getvalue()


def _cosine_similarity(a, b) -> float:
    """Cosine similarity between two 1-D numpy arrays."""
    import numpy as np

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _match_speaker(
    embedding,
    speakers: dict[str, list],
    threshold: float,
) -> str | None:
    """Match an embedding against reference speakers via cosine similarity.

    Returns the best-matching speaker's name if the max cosine similarity
    exceeds ``threshold``, else ``None`` (caller assigns a generic label).
    """
    best_name: str | None = None
    best_score = -1.0
    for name, ref_embeddings in speakers.items():
        for ref in ref_embeddings:
            score = _cosine_similarity(embedding, ref)
            if score > best_score:
                best_score = score
                best_name = name
    if best_score >= threshold:
        return best_name
    return None


async def identify_speakers(wav_bytes: bytes) -> str:
    """Diarize + transcribe + label each turn. Returns a diarized transcript.

    Returns a string like::

        Jake: hey let's ship the speaker ID feature.
        Speaker 1: sounds good, I'll review the PR.
        Jake: thanks.

    Returns ``""`` on ANY failure (pyannote not installed, model load
    failure, diarization failure) so callers fall back to the anonymous
    path. Heavy steps (diarization, embedding) are wrapped in
    ``asyncio.to_thread`` so the event loop isn't blocked.

    pyannote.audio 4.x: ``SpeakerDiarization.__call__`` returns a
    ``DiarizeOutput`` dataclass (NOT an ``Annotation`` directly). We unwrap
    ``exclusive_speaker_diarization`` for turn iteration (strips overlapping
    speech so per-turn Whisper slices aren't garbled by cross-talk) and
    reuse the precomputed ``speaker_embeddings`` (np.ndarray, one row per
    speaker, sorted in ``speaker_diarization.labels()`` order) to match
    each speaker once against the reference set — no per-turn re-embedding.
    """
    from opencode_discord_bot.voice import transcribe_audio

    # Lazily construct the models (heavy, in threads). The embedding model
    # is still needed by load_speakers() to compute reference embeddings.
    pipeline = await asyncio.to_thread(_get_pyannote_pipeline)
    await asyncio.to_thread(_get_speaker_embedding)
    speakers = await asyncio.to_thread(load_speakers)
    if not speakers:
        return ""

    # Write WAV to a temp file (pyannote reads from a path, not bytes —
    # same constraint as faster-whisper at voice.py:241-243).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name
    try:
        # Run diarization (heavy, in a thread). 4.x returns DiarizeOutput.
        out = await asyncio.to_thread(pipeline, tmp_path)

        # Unwrap the DiarizeOutput dataclass (4.x). Fall back to the
        # legacy Annotation return shape (3.x) defensively in case a
        # future 3.x install runs this code.
        if hasattr(out, "exclusive_speaker_diarization"):
            diarization = out.exclusive_speaker_diarization
            labels_in_order = (
                out.speaker_diarization.labels()
                if hasattr(out, "speaker_diarization")
                else diarization.labels()
            )
            speaker_embeddings = out.speaker_embeddings
        else:
            diarization = out
            labels_in_order = diarization.labels()
            speaker_embeddings = None

        if speaker_embeddings is None:
            _log.warning(
                "DiarizeOutput.speaker_embeddings is None — cannot match "
                "speakers against references; returning anonymous"
            )
            return ""

        # Match each pyannote speaker label against the reference set once,
        # using the precomputed per-speaker embedding (one row per label,
        # sorted in labels_in_order order). Unknowns get generic Speaker N
        # labels in encounter order (index+1 of first unknown).
        import numpy as np  # noqa: F401 — used via np.asarray below

        label_map: dict[str, str] = {}
        unknown_counter = 0
        emb_arr = np.asarray(speaker_embeddings)
        for idx, speaker_label in enumerate(labels_in_order):
            if idx >= emb_arr.shape[0]:
                # Defensive: labels/embeddings length mismatch — treat as
                # unknown so the turn still gets a label.
                unknown_counter += 1
                label_map[speaker_label] = f"Speaker {unknown_counter}"
                continue
            turn_emb = emb_arr[idx]
            try:
                matched = _match_speaker(
                    turn_emb, speakers, config.speaker_match_threshold
                )
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "embedding match failed for %s: %r", speaker_label, e
                )
                matched = None
            if matched is not None:
                label_map[speaker_label] = matched
            else:
                unknown_counter += 1
                label_map[speaker_label] = f"Speaker {unknown_counter}"

        # Transcribe each turn slice and emit the labeled lines.
        lines: list[str] = []
        for turn, _, speaker_label in diarization.itertracks(yield_label=True):
            slice_bytes = _slice_wav(wav_bytes, turn.start, turn.end)
            if not slice_bytes:
                continue
            try:
                text = (await transcribe_audio(slice_bytes) or "").strip()
                if not text:
                    continue
            except Exception as e:  # noqa: BLE001
                _log.warning("per-turn transcription failed: %r", e)
                continue

            display_label = label_map.get(speaker_label, f"Speaker {unknown_counter}")
            lines.append(f"{display_label}: {text}")

        return "\n".join(lines)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def transcribe_with_speakers(wav_bytes: bytes) -> str:
    """Async orchestrator: diarized transcript if available, else anonymous.

    - If ``config.speaker_id_enabled`` is False, OR ``load_speakers()``
      returns ``{}`` (no ``Speakers/`` folder or empty), call
      ``voice.transcribe_audio`` (the existing anonymous path).
    - Otherwise call ``identify_speakers``; if it returns empty (failure
      or no speech), fall back to ``voice.transcribe_audio``.

    Always returns a non-empty string when speech is detected, matching
    the contract of the existing ``transcribe_audio`` callers.
    """
    from opencode_discord_bot.voice import transcribe_audio

    if not config.speaker_id_enabled:
        return await transcribe_audio(wav_bytes)

    # Check if reference speakers are available (cheap with the cache).
    try:
        speakers = await asyncio.to_thread(load_speakers)
    except Exception as e:  # noqa: BLE001
        _log.warning("load_speakers failed (%r); falling back to anonymous", e)
        return await transcribe_audio(wav_bytes)

    if not speakers:
        _log.warning(
            "speaker_id_enabled is True but load_speakers() returned {} — "
            "falling back to anonymous STT; verify SPEAKERS_DIR points at a "
            "populated Speakers/ folder resolvable from the bot's launch cwd"
        )
        return await transcribe_audio(wav_bytes)

    try:
        diarized = await identify_speakers(wav_bytes)
        if diarized:
            return diarized
    except Exception as e:  # noqa: BLE001
        _log.warning("speaker ID failed (%r); falling back to anonymous", e)

    return await transcribe_audio(wav_bytes)