"""Config tests: channel-key Telegram routing."""

import pytest

from bot.config import Config, ConfigError


@pytest.fixture()
def base_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("DEEPL_API_KEY", "key")
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    monkeypatch.delenv("GERMAN_TELEGRAM_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ENGLISH_TELEGRAM_CHANNEL_ID", raising=False)
    return monkeypatch


def test_channels_by_key(base_env):
    base_env.setenv("GERMAN_TELEGRAM_CHANNEL_ID", "@german")
    base_env.setenv("ENGLISH_TELEGRAM_CHANNEL_ID", "@english")
    base_env.setenv("BREAKING_NEWS_TELEGRAM_CHANNEL_ID", "@breaking")
    config = Config.from_env()
    assert config.channel_for("german") == "@german"
    assert config.channel_for("english") == "@english"
    assert config.channel_for("breaking_news") == "@breaking"
    assert config.channel_for("french") is None


def test_default_channel_is_key_fallback(base_env):
    base_env.setenv("ENGLISH_TELEGRAM_CHANNEL_ID", "@english")
    base_env.setenv("TELEGRAM_CHANNEL_ID", "@everything")
    config = Config.from_env()
    assert config.channel_for("english") == "@english"
    assert config.channel_for("german") == "@everything"


def test_no_channels_config_loads_but_resolves_none(base_env):
    # Loading succeeds; the per-source channel check in main() reports
    # which channel keys are missing.
    config = Config.from_env()
    assert config.channel_for("german") is None


def test_missing_required_vars(base_env):
    base_env.delenv("TELEGRAM_BOT_TOKEN")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_queue_intervals(base_env):
    config = Config.from_env()
    assert config.en_post_interval_minutes == 15
    assert config.translated_post_interval_minutes == 60

    base_env.setenv("EN_POST_INTERVAL_MINUTES", "5")
    base_env.setenv("TRANSLATED_POST_INTERVAL_MINUTES", "120")
    config = Config.from_env()
    assert config.en_post_interval_minutes == 5
    assert config.translated_post_interval_minutes == 120
