"""Frozen pydantic models for the version-2 spec.

Field defaults here are the documented defaults. Resolution
(resolve.py) distinguishes explicitly-set fields from defaults via
``model_fields_set``, so the precedence chain
``documented default < spec defaults < channel registry < binding``
works without sentinel values.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceKind = Literal["general_news", "central_bank", "statistics", "press_wire", "exchange"]
PostStyle = Literal["photo_full", "link_preview", "text_only"]
QueuePolicy = Literal["drop_oldest", "drain_all"]

_COLD_START_RE = re.compile(r"^(skip_all|post_newest:[1-9]\d*)$")


class FrozenModel(BaseModel):
    """Base for all spec models: immutable, unknown keys rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Mapping(FrozenModel):
    """Per-source extraction rules: expressions locating each item field
    in the feed XML, plus lead/category post-processing knobs. The
    expression grammar is documented in fetcher.mapping."""

    items: str
    id: str
    id_pattern: str | None = None
    title: str
    url: str
    published: str | None = None
    published_format: Literal["rfc822", "iso8601"] | None = None
    lead: str | tuple[str, ...] | None = None
    allow_empty_lead: bool = False
    lead_fallback: Literal["title"] | None = None
    lead_html: bool = False
    lead_remove: tuple[str, ...] = ()
    image: str | tuple[str, ...] | None = None
    category: str | None = None
    category_from: str | None = None
    copyright: str | None = None

    @field_validator("category_from")
    @classmethod
    def _check_category_from(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"url_path_segment:\d+", v):
            raise ValueError(f"unsupported category_from {v!r}; only url_path_segment:N")
        return v

    @field_validator("lead_remove")
    @classmethod
    def _check_lead_remove(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in v:
            re.compile(pattern)
        return v


class WhenClause(FrozenModel):
    """Content condition on a binding: keyword/category/kind tests an item
    must pass to be routed to the channel. Empty groups are no-ops."""

    keywords_any: tuple[str, ...] = ()
    keywords_all: tuple[str, ...] = ()
    keywords_none: tuple[str, ...] = ()
    categories_any: tuple[str, ...] = ()
    source_kinds: tuple[SourceKind, ...] = ()
    match_fields: tuple[Literal["title", "lead"], ...] = ("title", "lead")
    case_sensitive: bool = False


class Binding(FrozenModel):
    """Source→channel binding. Only binding-scoped keys are modelled;
    channel-scoped keys (id, post_interval_min, max_posts_per_run,
    max_age_min) are rejected by the loader before pydantic runs."""

    name: str
    include_categories: tuple[str, ...] = ()
    exclude_categories: tuple[str, ...] = ()
    lead_max_length: int = 300
    translate_lead: bool = False
    post_style: PostStyle | None = None  # None → channel's post_style
    when: WhenClause | None = None


class ChannelDef(FrozenModel):
    """Channel registry entry: a Telegram chat and its posting knobs."""

    name: str
    id: str
    language: str = "en"
    post_interval_min: int = 30
    max_posts_per_run: int = 5
    max_age_min: int | None = None
    queue_policy: QueuePolicy = "drop_oldest"
    post_style: PostStyle = "link_preview"
    enabled: bool = True


class SourceDef(FrozenModel):
    """A feed source: where and how often to fetch, how to map items,
    and the channel bindings that receive them."""

    name: str
    type: Literal["xml"] = "xml"
    url: str
    kind: SourceKind = "general_news"
    region: str | None = None
    language: str
    fetch_limit: int = 10
    fetch_interval_min: int = 15
    cold_start_policy: str = "skip_all"
    enabled: bool = True
    url_verified: str | None = None
    namespaces: dict[str, str] = {}
    schema_file: str | None = None
    channels: tuple[Binding, ...]
    mapping: Mapping
    notes: str | None = None

    @field_validator("cold_start_policy")
    @classmethod
    def _check_cold_start(cls, v: str) -> str:
        if not _COLD_START_RE.fullmatch(v):
            raise ValueError(
                f"unsupported cold_start_policy {v!r}; use skip_all or post_newest:N"
            )
        return v

    @field_validator("channels")
    @classmethod
    def _check_channels(cls, v: tuple[Binding, ...]) -> tuple[Binding, ...]:
        if not v:
            raise ValueError("source must bind to at least one channel")
        return v


# --- error handling and operator alerting ------------------------------

Severity = Literal["critical", "error", "warning", "info"]

SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "error": 2, "critical": 3}


class ClassificationRule(FrozenModel):
    """Severity of an error class, optionally escalating to a higher
    severity after ``escalate_after`` consecutive occurrences."""

    severity: Severity
    escalate_after: int | None = None
    escalates_to: Severity | None = None


# Default error-class → severity mapping. Overridable per class and
# per source in spec.errors; unknown classes fall back to UNKNOWN_CLASS_RULE.
DEFAULT_CLASSIFICATION: dict[str, ClassificationRule] = {
    "SpecValidationFailed": ClassificationRule(severity="critical", escalate_after=1),
    "SchemaValidationFailed": ClassificationRule(severity="critical", escalate_after=1),
    "DatabaseError": ClassificationRule(severity="critical", escalate_after=1),
    "TelegramForbidden": ClassificationRule(severity="critical", escalate_after=1),
    "TelegramBadRequest": ClassificationRule(severity="error", escalate_after=1),
    "FeedParseError": ClassificationRule(severity="error", escalate_after=2, escalates_to="critical"),
    "FetchHttpError4xx": ClassificationRule(severity="error", escalate_after=2, escalates_to="critical"),
    "FetchHttpError5xx": ClassificationRule(severity="warning", escalate_after=5, escalates_to="error"),
    "FetchTimeout": ClassificationRule(severity="warning", escalate_after=5, escalates_to="error"),
    "FetchNetworkError": ClassificationRule(severity="warning", escalate_after=5, escalates_to="critical"),
    "MappingRequiredFieldMissing": ClassificationRule(severity="error", escalate_after=2, escalates_to="critical"),
    "MappingOptionalFieldMissing": ClassificationRule(severity="info"),
    "PublishedDateUnparseable": ClassificationRule(severity="warning", escalate_after=3, escalates_to="error"),
    "TranslationProviderError": ClassificationRule(severity="warning", escalate_after=3, escalates_to="error"),
    "TranslationQuotaExceeded": ClassificationRule(severity="error", escalate_after=1),
    "TelegramRateLimited": ClassificationRule(severity="info", escalate_after=10, escalates_to="warning"),
    "PossibleGap": ClassificationRule(severity="warning", escalate_after=3, escalates_to="error"),
    "ColdStartSkipped": ClassificationRule(severity="info"),
    "UnauthorizedCommand": ClassificationRule(severity="warning", escalate_after=5),
    # The public webhook endpoint will be scanned; per-fingerprint grouping
    # keeps this to one event, escalation only fires on a sustained probe.
    "UnauthorizedWebhookCall": ClassificationRule(
        severity="warning", escalate_after=20, escalates_to="error"
    ),
}

UNKNOWN_CLASS_RULE = ClassificationRule(severity="error", escalate_after=3)


class AuthorizationCfg(FrozenModel):
    """Who may run read and write ops-bot commands."""

    read_roles: tuple[str, ...] = ("member",)
    write_roles: tuple[str, ...] = ("admin",)
    write_allowlist: tuple[int, ...] = ()
    admin_cache_min: int = 15


class QuietHours(FrozenModel):
    """Daily window during which alerts below ``suppress_below`` are held."""

    from_time: str = Field(alias="from")
    to: str
    timezone: str = "UTC"
    suppress_below: Severity = "critical"

    @field_validator("from_time", "to")
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", v):
            raise ValueError(f"quiet_hours time {v!r} must be HH:MM")
        return v


class AlertingCfg(FrozenModel):
    """Operator alerting behaviour: escalation ladder, rate limits,
    digests, quiet hours."""

    enabled: bool = True
    escalation_ladder: tuple[int, ...] = (1, 10, 50, 200)
    cooldown_min: int = 30
    max_alerts_per_hour: int = 10
    digest_interval_min: int = 360
    silent_below: Severity = "error"
    edit_on_escalation: bool = True
    pin_critical: bool = True
    quiet_hours: QuietHours | None = None


class RetentionCfg(FrozenModel):
    """How long error occurrences and events are kept."""

    occurrences_days: int = 30
    events_days: int = 365


class ErrorsConfig(FrozenModel):
    """The spec's ``errors`` section: ops chat, command authorization,
    alerting, and per-class / per-source classification overrides."""

    ops_chat_id: str
    ops_topic_id: int | None = None
    digest_topic_id: int | None = None
    authorization: AuthorizationCfg = AuthorizationCfg()
    alerting: AlertingCfg = AlertingCfg()
    classification: dict[str, ClassificationRule] = {}
    source_overrides: dict[str, dict[str, ClassificationRule]] = {}
    retention: RetentionCfg = RetentionCfg()


class Spec(FrozenModel):
    """The whole version-2 spec document."""

    version: Literal[2]
    defaults: dict[str, Any] = {}
    channels: tuple[ChannelDef, ...]
    sources: tuple[SourceDef, ...]
    errors: ErrorsConfig | None = None

    def channel(self, name: str) -> ChannelDef:
        """Registry entry by name; raises KeyError for an unknown channel."""
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(name)


def parse_cold_start(policy: str) -> tuple[Literal["skip_all", "post_newest"], int]:
    """"skip_all" → ("skip_all", 0); "post_newest:N" → ("post_newest", N)."""
    if policy == "skip_all":
        return "skip_all", 0
    kind, _, n = policy.partition(":")
    return "post_newest", int(n)
