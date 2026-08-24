# opencode-discord-bot — Fly.io Dockerfile
#
# Two-stage build. Stage 1 installs the bot (and its Python deps) from the
# local source into a venv. Stage 2 copies the venv into a slim runtime image,
# adds ffmpeg (for /oc_talk + voice-message extraction), and adds the Tailscale
# binaries so the container can join a tailnet and reach a remote opencode serve
# (e.g. on the user's desktop) over a private WireGuard tunnel.
#
# The `opencode` binary is intentionally NOT installed. The bot auto-spawns
# `opencode serve` only when OPENCODE_SERVE_ENABLED=true (default). On Fly we
# set OPENCODE_SERVE_ENABLED=false and point OPENCODE_SERVER_URL at the user's
# desktop opencode serve over Tailscale, so the binary is never needed. If you
# ever want the bot to spawn its own server in-container, install opencode
# (npm install -g opencode-ai) and set OPENCODE_SERVE_ENABLED=true.
#
# The `speakers` extra (pyannote.audio + torch, ~1-2GB) is NOT installed —
# speaker ID degrades gracefully to anonymous transcripts without it. Add
# `pip install '/src[speakers]'` to the builder stage + bump the machine to
# 4GB RAM if you want diarization.

# ---------- Stage 1: build venv with the bot + deps ----------
FROM python:3.13-slim AS builder

WORKDIR /src

# Copy the local source (the build context is the repo root of
# opencode-discord-bot/, so pyproject.toml + src/ land here). The
# .dockerignore excludes .venv, Speakers/, tests/, etc.
COPY . /src/

# Build a venv with the bot + its deps. `--system-site-packages` is NOT used
# so the venv is self-contained; we copy it wholesale into the runtime stage.
# The `sed` strips the `force-include` line from pyproject.toml because newer
# hatchling already includes non-.py files from `packages`, so the
# force-include duplicates `agent/oc-assistant.md` and the wheel build fails
# with "A second file is being added to the wheel archive at the same path".
# The `.md` file is still shipped (it's under `src/opencode_discord_bot/agent/`
# which is in `packages`).
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --upgrade pip \
    && sed -i '/^force-include = /d' /src/pyproject.toml \
    && /venv/bin/pip install --no-cache-dir /src

# ---------- Stage 2: runtime image ----------
FROM python:3.13-slim

# ffmpeg is a hard runtime dep for /oc_talk + voice-message audio extraction
# (extract_audio_to_wav subprocess) and TTS playback. Not needed for the
# text-only command surface (/oc, /oc_plan, follow-ups), but the bot imports
# the voice pipeline unconditionally at module top, so ffmpeg must be present
# to avoid import-time failures in the broader package graph. ca-certificates
# is needed for HTTPS to Discord + Comulytic + OpenAI + Tailscale's coord
# server. iptables is required by Tailscale for subnet routing on Linux.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        iptables \
    && rm -rf /var/lib/apt/lists/*

# Copy the venv from the builder stage.
COPY --from=builder /venv /venv

# Copy Tailscale binaries from the official Tailscale Docker image. We need
# both `tailscaled` (the daemon) and `tailscale` (the CLI). The daemon is
# started by start.sh before the bot so the container joins the tailnet and
# can reach the user's desktop opencode serve over a private IP.
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscale  /usr/local/bin/tailscale

# Tailscale state directories. /var/run/tailscale is the socket dir (ephemeral,
# fine in-container). /var/lib/tailscale holds the daemon state — we point
# --state at /data/tailscaled.state in start.sh so it persists on the Fly
# volume and the node identity survives restarts (matters less with an
# ephemeral auth key, but keeps re-auth fast + stable).
RUN mkdir -p /var/run/tailscale /var/lib/tailscale

# Copy the entrypoint script + the Fly .env template (seeded to /data/.env
# on first boot so /oc_setup has a base file to update rather than creating
# one from scratch with only the guild fields).
COPY start.sh /start.sh
COPY fly.env.example /app/fly.env.example
RUN chmod +x /start.sh

# /data is the Fly volume mount point (see fly.toml [[mounts]]). The bot's
# working directory is /data so .env, .opencode-discord-bot-sessions.json,
# .opencode-discord-bridge-sessions.json, .comulytic-seen.json, and the
# faster-whisper model cache (HF_HOME=/data/hf-cache) all persist across
# restarts. start.sh `cd /data` before launching the bot.
WORKDIR /data

# No inbound services — the bot is outbound-only (Discord gateway, Tailscale,
# opencode serve over Tailscale, Comulytic API, optional OpenAI). No
# EXPOSE needed.
CMD ["/start.sh"]