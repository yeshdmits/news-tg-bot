"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


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


def _get_category_list(name: str) -> tuple[str, ...]:
    """Comma-separated category paths, normalized to hashtag form
    (lowercase, '/' and '-' become '_') to match Article.category."""
    raw = os.environ.get(name, "")
    items = []
    for part in raw.split(","):
        part = part.strip().strip("/").lower().replace("/", "_").replace("-", "_")
        if part:
            items.append(part)
    return tuple(items)


def _matches_any(category: str, prefixes: tuple[str, ...]) -> bool:
    """True if the category equals a prefix or is a subcategory of one."""
    return any(
        category == prefix or category.startswith(prefix + "_")
        for prefix in prefixes
    )


POST_STYLES = ("photo_full", "photo_short", "text_full", "text_short")


def _get_post_style(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip().lower() or default
    if raw not in POST_STYLES:
        raise ConfigError(
            f"{name} must be one of {', '.join(POST_STYLES)}, got {raw!r}"
        )
    return raw


_CHANNEL_VAR_RE = re.compile(r"^([A-Z0-9_]{2,32})_TELEGRAM_CHANNEL_ID$")


def _get_channel_ids() -> dict[str, str]:
    """Channel ids from <KEY>_TELEGRAM_CHANNEL_ID variables, keyed by the
    lowercased KEY — matched against each source's 'channel' in sources.json."""
    channels = {}
    for name, value in os.environ.items():
        match = _CHANNEL_VAR_RE.match(name)
        if match and value.strip():
            channels[match.group(1).lower()] = value.strip()
    return channels


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    deepl_api_key: str

    # channel key -> channel id, from <KEY>_TELEGRAM_CHANNEL_ID env vars.
    telegram_channel_ids: dict[str, str] = field(default_factory=dict)
    # Fallback for channel keys without a dedicated variable (TELEGRAM_CHANNEL_ID).
    default_channel_id: str | None = None

    # Posting-queue intervals: English sources need no translation and post
    # more often; sources requiring translation post on the slower cadence.
    en_post_interval_minutes: int = 15
    translated_post_interval_minutes: int = 60
    news_fetch_limit: int = 10
    db_path: str = "data/posted.db"
    sources_path: str = "bot/sources.json"
    translate_lead: bool = True
    lead_max_chars: int = 300
    max_posts_per_cycle: int = 5
    skip_initial_backlog: bool = True
    skip_categories: tuple[str, ...] = ()
    include_categories: tuple[str, ...] = ()
    post_style: str = "photo_full"

    def channel_for(self, channel_key: str) -> str | None:
        """The Telegram channel id behind a source's 'channel' key."""
        return self.telegram_channel_ids.get(channel_key) or self.default_channel_id

    @property
    def post_with_image(self) -> bool:
        return self.post_style.startswith("photo")

    @property
    def post_full_text(self) -> bool:
        return self.post_style.endswith("full")

    def is_category_allowed(self, category: str) -> bool:
        """Category filter, matching by prefix (an entry covers its subcategories).

        INCLUDE_CATEGORIES non-empty: only those categories pass (skip list
        is ignored). Otherwise SKIP_CATEGORIES non-empty: everything but those
        passes. Both empty: everything passes.
        """
        if self.include_categories:
            return _matches_any(category, self.include_categories)
        return not _matches_any(category, self.skip_categories)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            deepl_api_key=_require("DEEPL_API_KEY"),
            telegram_channel_ids=_get_channel_ids(),
            default_channel_id=os.environ.get("TELEGRAM_CHANNEL_ID", "").strip() or None,
            en_post_interval_minutes=_get_int("EN_POST_INTERVAL_MINUTES", 15),
            translated_post_interval_minutes=_get_int(
                "TRANSLATED_POST_INTERVAL_MINUTES", 60
            ),
            news_fetch_limit=_get_int("NEWS_FETCH_LIMIT", 10),
            db_path=os.environ.get("DB_PATH", "data/posted.db"),
            sources_path=os.environ.get("SOURCES_PATH", "bot/sources.json"),
            translate_lead=_get_bool("TRANSLATE_LEAD", True),
            lead_max_chars=_get_int("LEAD_MAX_CHARS", 300),
            max_posts_per_cycle=_get_int("MAX_POSTS_PER_CYCLE", 5),
            skip_initial_backlog=_get_bool("SKIP_INITIAL_BACKLOG", True),
            skip_categories=_get_category_list("SKIP_CATEGORIES"),
            include_categories=_get_category_list("INCLUDE_CATEGORIES"),
            post_style=_get_post_style("POST_STYLE", "photo_full"),
        )
