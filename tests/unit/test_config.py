"""Unit tests for ``config.BotConfig`` + ``reload_config``."""

from opencode_discord_bot.config import BotConfig, config, reload_config


def test_defaults_all_safe():
    """The class-level field defaults are all safe/empty (no secrets committed).

    The bot dir ships a real ``.env`` (gitignored) with a Discord token, so
    a constructed ``BotConfig()`` reads non-empty values — but the field
    *defaults* (what a fresh install with no .env gets) are all safe.
    """
    fields = BotConfig.model_fields
    assert fields["discord_bot_token"].default == ""
    assert fields["discord_bot_guild_id"].default == 0
    assert fields["opencode_server_password"].default == ""
    assert fields["openai_api_key"].default == ""
    assert fields["ollama_auth_key"].default == ""
    assert fields["comulytic_enabled"].default is False
    assert fields["comulytic_jwt"].default == ""
    assert fields["voice_stt_provider"].default == "local"
    assert fields["voice_message_enabled"].default is True
    # Port default is 4097 (NOT 4096 — 4096 is the opencode-remote-gui Flet
    # UI port; pointing the bot at 4096 when the GUI is co-hosted makes the
    # health probe false-positive on the GUI's HTML and every API call 405s).
    # Pinning the default here catches future drift.
    assert fields["opencode_server_url"].default == "http://127.0.0.1:4097"
    assert fields["opencode_serve_port"].default == 4097


def test_module_singleton_is_botconfig_instance():
    assert isinstance(config, BotConfig)


def test_reload_config_re_reads_env(monkeypatch, tmp_path):
    """reload_config re-reads env vars and mutates the singleton in place."""
    # Move to a tmp dir with no .env so only env vars are read.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token-xyz")
    monkeypatch.setattr(config, "discord_bot_token", "old")
    reload_config()
    assert config.discord_bot_token == "test-token-xyz"


def test_reload_config_preserves_singleton_identity():
    before = config
    reload_config()
    assert config is before


def test_mutation_visible_via_attribute(monkeypatch):
    monkeypatch.setattr(config, "discord_bot_guild_id", 12345)
    assert config.discord_bot_guild_id == 12345