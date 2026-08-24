#!/bin/sh
# Container entrypoint for opencode-discord-bot on Fly.io.
#
# 1. Start the Tailscale daemon (tailscaled) in the background.
# 2. Join the tailnet using TAILSCALE_AUTHKEY (set via flyctl secrets).
# 3. cd to /data (the persistent Fly volume) and launch the bot.
#
# The bot's .env, session-router JSONs, Comulytic seen-set, and the
# faster-whisper model cache (HF_HOME=/data/hf-cache) all live on /data so
# they survive machine restarts. The bot is launched with
# OPENCODE_SERVE_ENABLED=false (set via flyctl secrets) so it does NOT try to
# spawn a local opencode serve — it talks to the user's desktop opencode
# serve over the Tailscale tunnel via OPENCODE_SERVER_URL.
#
# Tailscale is required: the bot needs to reach the desktop's opencode serve,
# which is NOT exposed to the public internet. The Tailscale auth key should
# be reusable + ephemeral + pre-authorized (see Tailscale admin console >
# Keys). Ephemeral means the node auto-removes from the tailnet when the
# machine stops, keeping the tailnet clean.

set -e

# Tailscale daemon state lives on the volume so the node identity is stable
# across restarts (faster re-auth, stable Tailscale IP within the ephemeral
# key's lifecycle).
TS_STATE=/data/tailscaled.state
TS_SOCKET=/var/run/tailscale/tailscaled.sock

echo "Starting tailscaled..."
tailscaled --state="$TS_STATE" --socket="$TS_SOCKET" &

# Give tailscaled a moment to bring the socket up before we run `tailscale up`.
# `tailscale up` will retry internally, but a short sleep avoids a noisy
# "socket not found" error on the first attempt.
sleep 2

echo "Joining tailnet (hostname=discord-bot)..."
tailscale up \
    --auth-key="$TAILSCALE_AUTHKEY" \
    --hostname=discord-bot \
    --accept-routes

echo "Tailscale is up. Tailscale IP:"
tailscale ip -4 || tailscale ip || true

# Seed /data/.env from the template if this is a fresh volume (first boot).
# /oc_setup writes guild-specific IDs (category, channel, guild) to .env at
# runtime, so the file must exist before the bot starts. Fly secrets (env
# vars) override .env values in pydantic-settings, so sensitive values
# (DISCORD_BOT_TOKEN, OPENCODE_SERVER_PASSWORD, COMULYTIC_JWT, etc.) stay
# in Fly secrets and are NOT in the .env template. The .env file holds only
# non-sensitive runtime config + the guild IDs /oc_setup discovers.
if [ ! -f /data/.env ]; then
    echo "First boot: seeding /data/.env from template..."
    cp /app/fly.env.example /data/.env
fi

# Launch the bot from /data so pydantic-settings finds .env (env_file=".env"
# is cwd-relative) and the session-router / seen-set files land on the
# volume. `exec` replaces the shell with the bot process so it becomes PID 1
# and receives Fly's shutdown signals directly.
cd /data
echo "Starting opencode-discord-bot..."
exec /venv/bin/python -m opencode_discord_bot