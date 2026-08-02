"""Row types for the archive tables. All datetimes are timezone-aware —
pydantic rejects naive values at this boundary, so a naive timestamp can
never reach the database."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class Decision(StrEnum):
    """Routing outcome recorded for an (item, channel) pair."""

    ROUTED = "routed"
    FILTERED_CATEGORY = "filtered_category"
    PREDICATE_FAILED = "predicate_failed"
    TOO_OLD = "too_old"
    DUPLICATE = "duplicate"
    CHANNEL_DISABLED = "channel_disabled"
    COLD_START_SKIP = "cold_start_skip"
    RATE_LIMITED = "rate_limited"


class DeliveryStatus(StrEnum):
    """Outcome of a Telegram delivery attempt."""

    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class FrozenRow(BaseModel):
    """Base class for immutable row models."""

    model_config = ConfigDict(frozen=True)


class ErrorState(StrEnum):
    """Review lifecycle of an error event."""

    UNACKED = "unacked"
    ACKED = "acked"
    RESOLVED = "resolved"


class SourceState(FrozenRow):
    """Per-source fetch scheduling row: next/last fetch times, HTTP cache
    validators, cold-start flag and the failure counter behind backoff."""

    source_name: str
    next_fetch_utc: AwareDatetime
    last_fetch_utc: AwareDatetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    cold_start_done: bool = False
    consecutive_failures: int = 0


class ChannelState(FrozenRow):
    """Per-channel posting row; last_post_utc drives the post-interval gate."""

    channel_name: str
    last_post_utc: AwareDatetime | None = None


class ItemRecord(FrozenRow):
    """One archived item as read back from the items table, categories attached."""

    item_id: UUID
    source_name: str
    source_item_id: str
    title: str
    lead_original: str | None
    lead_language: str | None
    url_raw: str
    canonical_url: str
    image_url: str | None
    published_raw: str | None
    published_utc: AwareDatetime | None
    first_seen_utc: AwareDatetime
    content_hash: bytes
    title_simhash: int
    duplicate_of: UUID | None
    copyright_holder: str | None
    spec_hash: str
    categories: tuple[str, ...] = ()
