"""Routing decisions and per-channel selection.

Pure logic — no I/O. The runner feeds it stored data and persists what it
returns.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import UUID

from archive.models import Decision
from feedspec.model import WhenClause
from feedspec.resolve import EffectiveChannel, EffectiveConfig


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of decide_item for one (item, channel) pair."""

    decision: Decision
    reason: str | None = None
    matched_clause: dict[str, Any] | None = None


def fold(text: str, *, case_sensitive: bool = False) -> str:
    """Accent-fold (NFKD, strip combining marks); casefold unless case_sensitive."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped if case_sensitive else stripped.casefold()


def keyword_match(keyword: str, text: str, *, case_sensitive: bool = False) -> bool:
    """Whole-word match of a (possibly multi-word) keyword in folded text."""
    needle = fold(keyword, case_sensitive=case_sensitive)
    haystack = fold(text, case_sensitive=case_sensitive)
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def evaluate_when(
    when: WhenClause,
    *,
    title: str,
    lead: str | None,
    categories: Sequence[str],
    source_kind: str,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """AND of all present clauses, against original-language text.

    Returns (passed, matched_clause, failed_clause). matched_clause records
    what satisfied each clause so routing_decisions stays auditable.
    """
    fields = {"title": title, "lead": lead or ""}
    text = "\n".join(fields[f] for f in when.match_fields)
    cs = when.case_sensitive
    matched: dict[str, Any] = {}

    if when.source_kinds:
        if source_kind not in when.source_kinds:
            return False, None, "source_kinds"
        matched["source_kinds"] = source_kind

    if when.categories_any:
        folded_cats = {fold(c) for c in categories}
        hits = [c for c in when.categories_any if fold(c) in folded_cats]
        if not hits:
            return False, None, "categories_any"
        matched["categories_any"] = hits

    if when.keywords_any:
        hits = [k for k in when.keywords_any if keyword_match(k, text, case_sensitive=cs)]
        if not hits:
            return False, None, "keywords_any"
        matched["keywords_any"] = hits

    if when.keywords_all:
        misses = [k for k in when.keywords_all if not keyword_match(k, text, case_sensitive=cs)]
        if misses:
            return False, None, "keywords_all"
        matched["keywords_all"] = list(when.keywords_all)

    if when.keywords_none:
        hits = [k for k in when.keywords_none if keyword_match(k, text, case_sensitive=cs)]
        if hits:
            return False, None, "keywords_none"
        matched["keywords_none"] = []

    return True, matched or None, None


def category_filtered(
    categories: Sequence[str],
    include: Sequence[str],
    exclude: Sequence[str],
) -> str | None:
    """Reason string when filtered, else None.

    An item with no categories is never filtered — absent categories
    no-op both lists, and an empty include list means "no include filter",
    never "include nothing".
    """
    if not categories:
        return None
    folded = {fold(c) for c in categories}
    excluded = [c for c in exclude if fold(c) in folded]
    if excluded:
        return f"excluded by category {excluded[0]!r}"
    if include and not any(fold(c) in folded for c in include):
        return f"not in include_categories {list(include)!r}"
    return None


def decide_item(
    cfg: EffectiveConfig,
    *,
    title: str,
    lead: str | None,
    categories: Sequence[str],
    published_utc: datetime | None,
    source_kind: str,
    now: datetime,
    is_duplicate: bool,
    duplicate_of: UUID | None = None,
    cold_start_eligible: bool = True,
    max_age_min: int | None,
) -> RouteDecision:
    """One routing decision for one (item, channel) pair.

    Decision precedence: duplicate → channel_disabled → cold_start_skip →
    too_old → filtered_category → predicate_failed → routed.
    """
    if is_duplicate:
        return RouteDecision(Decision.DUPLICATE, reason=str(duplicate_of) if duplicate_of else None)
    if not (cfg.channel_enabled and cfg.source_enabled):
        which = "channel" if not cfg.channel_enabled else "source"
        return RouteDecision(Decision.CHANNEL_DISABLED, reason=f"{which} disabled")
    if not cold_start_eligible:
        return RouteDecision(Decision.COLD_START_SKIP)
    if is_too_old(published_utc, max_age_min, now):
        return RouteDecision(
            Decision.TOO_OLD,
            reason=f"published {published_utc:%Y-%m-%dT%H:%M}Z, max_age_min={max_age_min}",
        )
    filtered = category_filtered(categories, cfg.include_categories, cfg.exclude_categories)
    if filtered:
        return RouteDecision(Decision.FILTERED_CATEGORY, reason=filtered)
    if cfg.when is not None:
        passed, matched, failed = evaluate_when(
            cfg.when, title=title, lead=lead, categories=categories, source_kind=source_kind
        )
        if not passed:
            return RouteDecision(Decision.PREDICATE_FAILED, reason=f"clause {failed}")
        return RouteDecision(Decision.ROUTED, matched_clause=matched)
    return RouteDecision(Decision.ROUTED)


def is_too_old(published_utc: datetime | None, max_age_min: int | None, now: datetime) -> bool:
    """max_age_min guard: applies always, not only at cold start. Items
    without a parseable published date are never "too old" — first_seen
    bounds them instead."""
    if published_utc is None or max_age_min is None:
        return False
    return now - published_utc > timedelta(minutes=max_age_min)


@dataclass(frozen=True)
class Candidate:
    """A routed, unposted item competing for a channel's posting window."""

    item_id: UUID
    source_name: str
    published_utc: datetime | None
    first_seen_utc: datetime


@dataclass(frozen=True)
class Selection:
    """Result of select_for_channel: what posts now, and what happens to
    the overflow."""

    to_post: tuple[Candidate, ...] = ()
    # drop_oldest overflow — flip routed → rate_limited, terminal
    dropped: tuple[Candidate, ...] = ()
    # drain_all overflow — stays routed, drained on later runs
    deferred: tuple[Candidate, ...] = ()
    gated: bool = field(default=False)  # post_interval_min not yet elapsed


def select_for_channel(
    candidates: Sequence[Candidate],
    channel: EffectiveChannel,
    *,
    last_post_utc: datetime | None,
    now: datetime,
    source_order: Mapping[str, int],
) -> Selection:
    """Per-chat selection over the merged candidate set from all sources.

    Ordering: published_utc ascending, spec source order as the stable
    tiebreak. The per-run cap and the post interval are enforced per chat id.
    While the interval gate is closed nothing posts and nothing is dropped —
    candidates stay routed for the next open run.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (
            c.published_utc or c.first_seen_utc,
            source_order.get(c.source_name, len(source_order)),
        ),
    )
    if last_post_utc is not None and now - last_post_utc < timedelta(
        minutes=channel.post_interval_min
    ):
        return Selection(deferred=tuple(ordered), gated=True)

    cap = channel.max_posts_per_run
    if len(ordered) <= cap:
        return Selection(to_post=tuple(ordered))
    if channel.queue_policy == "drop_oldest":
        return Selection(to_post=tuple(ordered[-cap:]), dropped=tuple(ordered[:-cap]))
    return Selection(to_post=tuple(ordered[:cap]), deferred=tuple(ordered[cap:]))
