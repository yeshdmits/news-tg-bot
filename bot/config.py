"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_channel_id: str
    deepl_api_key: str

    poll_interval_minutes: int = 30
    news_fetch_limit: int = 10
    db_path: str = "data/posted.db"
    translate_lead: bool = True
    lead_max_chars: int = 300
    max_posts_per_cycle: int = 5
    skip_initial_backlog: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_channel_id=_require("TELEGRAM_CHANNEL_ID"),
            deepl_api_key=_require("DEEPL_API_KEY"),
            poll_interval_minutes=_get_int("POLL_INTERVAL_MINUTES", 30),
            news_fetch_limit=_get_int("NEWS_FETCH_LIMIT", 10),
            db_path=os.environ.get("DB_PATH", "data/posted.db"),
            translate_lead=_get_bool("TRANSLATE_LEAD", True),
            lead_max_chars=_get_int("LEAD_MAX_CHARS", 300),
            max_posts_per_cycle=_get_int("MAX_POSTS_PER_CYCLE", 5),
            skip_initial_backlog=_get_bool("SKIP_INITIAL_BACKLOG", True),
        )
