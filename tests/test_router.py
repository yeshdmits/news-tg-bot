"""Routing tests: keyword matching, ``when`` predicate evaluation, per-item
routing decisions, and per-channel selection (ordering, caps, queue policies,
interval gating)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from archive.models import Decision
from feedspec.model import WhenClause
from feedspec.resolve import EffectiveChannel, EffectiveConfig
from newsbot.router import (
    Candidate,
    decide_item,
    evaluate_when,
    fold,
    keyword_match,
    select_for_channel,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def make_cfg(**overrides) -> EffectiveConfig:
    values = dict(
        source_name="src",
        channel_name="ch",
        chat_id="-1001",
        channel_language="en",
        post_interval_min=30,
        max_posts_per_run=5,
        max_age_min=720,
        queue_policy="drop_oldest",
        channel_enabled=True,
        source_enabled=True,
        include_categories=(),
        exclude_categories=(),
        lead_max_length=300,
        translate_lead=False,
        post_style="link_preview",
        when=None,
    )
    values.update(overrides)
    return EffectiveConfig(**values)


def make_channel(**overrides) -> EffectiveChannel:
    values = dict(
        name="ch",
        chat_id="-1001",
        language="en",
        post_interval_min=30,
        max_posts_per_run=5,
        max_age_min=None,
        queue_policy="drop_oldest",
        enabled=True,
    )
    values.update(overrides)
    return EffectiveChannel(**values)


def decide(cfg, **overrides):
    values = dict(
        title="A headline",
        lead="A lead",
        categories=(),
        published_utc=NOW - timedelta(minutes=5),
        source_kind="general_news",
        now=NOW,
        is_duplicate=False,
        max_age_min=cfg.max_age_min,
    )
    values.update(overrides)
    return decide_item(cfg, **values)


# --- folding and matching -------------------------------------------------


def test_fold_accents_and_case():
    assert fold("Zürich WÄHLT") == "zurich wahlt"
    assert fold("Zürich", case_sensitive=True) == "Zurich"


def test_keyword_whole_word():
    assert keyword_match("rate", "The interest rate rises")
    assert not keyword_match("rate", "The moderate wing")  # substring must not match
    assert keyword_match("interest rate", "the Interest Rate decision")
    assert keyword_match("inflación", "worries about inflacion grow")


# --- when predicate -------------------------------------------------------


def test_when_all_present_clauses_must_pass():
    when = WhenClause(
        keywords_any=("inflation", "interest rate"),
        keywords_none=("obituary",),
        source_kinds=("central_bank",),
    )
    passed, matched, _ = evaluate_when(
        when,
        title="ECB interest rate decision",
        lead=None,
        categories=(),
        source_kind="central_bank",
    )
    assert passed
    assert matched == {"keywords_any": ["interest rate"], "keywords_none": [],
                       "source_kinds": "central_bank"}

    failed, _, clause = evaluate_when(
        when,
        title="ECB interest rate decision",
        lead=None,
        categories=(),
        source_kind="general_news",
    )
    assert not failed
    assert clause == "source_kinds"


def test_when_matches_original_language_text_accent_folded():
    when = WhenClause(keywords_any=("zurich",))
    passed, _, _ = evaluate_when(
        when, title="Zürich: Polizei sperrt Autobahn", lead=None,
        categories=(), source_kind="general_news",
    )
    assert passed


def test_when_match_fields_limits_scope():
    when = WhenClause(keywords_any=("inflation",), match_fields=("title",))
    passed, _, _ = evaluate_when(
        when, title="Sports results", lead="inflation mentioned only in lead",
        categories=(), source_kind="general_news",
    )
    assert not passed


# --- decide_item ----------------------------------------------------------


def test_category_filter_noop_without_categories():
    """A source with no category mapping (→ items without categories) bound
    to a channel with exclude_categories posts normally."""
    cfg = make_cfg(exclude_categories=("sport",))
    assert decide(cfg, categories=()).decision is Decision.ROUTED


def test_empty_include_list_is_no_filter():
    """An empty include_categories list means no filter — it must not filter
    everything out."""
    cfg = make_cfg(include_categories=())
    assert decide(cfg, categories=("Politics",)).decision is Decision.ROUTED


def test_exclude_category_case_insensitive():
    cfg = make_cfg(exclude_categories=("sport",))
    result = decide(cfg, categories=("Sport", "News"))
    assert result.decision is Decision.FILTERED_CATEGORY


def test_include_category_must_intersect():
    cfg = make_cfg(include_categories=("economy",))
    assert decide(cfg, categories=("Economy",)).decision is Decision.ROUTED
    assert decide(cfg, categories=("Weather",)).decision is Decision.FILTERED_CATEGORY


def test_decision_priority_duplicate_first():
    cfg = make_cfg(channel_enabled=False)
    dup = uuid4()
    result = decide(cfg, is_duplicate=True, duplicate_of=dup)
    assert result.decision is Decision.DUPLICATE
    assert result.reason == str(dup)


def test_channel_disabled():
    result = decide(make_cfg(channel_enabled=False))
    assert result.decision is Decision.CHANNEL_DISABLED


def test_cold_start_skip():
    result = decide(make_cfg(), cold_start_eligible=False)
    assert result.decision is Decision.COLD_START_SKIP


def test_too_old_always_applies():
    cfg = make_cfg()
    result = decide(cfg, published_utc=NOW - timedelta(hours=13))
    assert result.decision is Decision.TOO_OLD
    # no published date → never too old
    assert decide(cfg, published_utc=None).decision is Decision.ROUTED


def test_predicate_failure_recorded():
    cfg = make_cfg(when=WhenClause(keywords_any=("inflation",)))
    result = decide(cfg, title="Weather tomorrow", lead=None)
    assert result.decision is Decision.PREDICATE_FAILED
    assert "keywords_any" in result.reason


# --- selection -------------------------------------------------


def cand(source, minutes_ago, item_id=None):
    return Candidate(
        item_id=item_id or uuid4(),
        source_name=source,
        published_utc=NOW - timedelta(minutes=minutes_ago),
        first_seen_utc=NOW,
    )


def test_cap_enforced_per_chat_across_sources():
    """The per-run cap covers the merged candidate set from all sources bound
    to the chat, not each source separately."""
    candidates = [cand("swissinfo", i) for i in range(4)] + [
        cand("blick", i + 10) for i in range(4)
    ]
    selection = select_for_channel(
        candidates,
        make_channel(max_posts_per_run=5),
        last_post_utc=None,
        now=NOW,
        source_order={"swissinfo": 0, "blick": 1},
    )
    assert len(selection.to_post) == 5
    assert len(selection.dropped) == 3


def test_ordering_published_asc_then_source_order():
    a = cand("blick", 30)
    b = cand("swissinfo", 30)  # same published_utc → spec order breaks the tie
    c = cand("swissinfo", 60)
    selection = select_for_channel(
        [a, b, c],
        make_channel(),
        last_post_utc=None,
        now=NOW,
        source_order={"swissinfo": 0, "blick": 1},
    )
    assert [x.item_id for x in selection.to_post] == [c.item_id, b.item_id, a.item_id]


def test_drop_oldest_keeps_newest():
    candidates = [cand("s", minutes_ago) for minutes_ago in (50, 40, 30, 20, 10, 5)]
    selection = select_for_channel(
        candidates,
        make_channel(max_posts_per_run=2, queue_policy="drop_oldest"),
        last_post_utc=None,
        now=NOW,
        source_order={"s": 0},
    )
    posted_ages = [(NOW - c.published_utc).total_seconds() / 60 for c in selection.to_post]
    assert posted_ages == [10, 5]  # newest two, still chronological
    assert len(selection.dropped) == 4


def test_drain_all_defers_newest():
    candidates = [cand("s", minutes_ago) for minutes_ago in (50, 40, 30)]
    selection = select_for_channel(
        candidates,
        make_channel(max_posts_per_run=2, queue_policy="drain_all"),
        last_post_utc=None,
        now=NOW,
        source_order={"s": 0},
    )
    posted_ages = [(NOW - c.published_utc).total_seconds() / 60 for c in selection.to_post]
    assert posted_ages == [50, 40]  # oldest first — the queue drains in order
    assert len(selection.deferred) == 1
    assert not selection.dropped


def test_interval_gate_blocks_without_dropping():
    candidates = [cand("s", 10)]
    selection = select_for_channel(
        candidates,
        make_channel(post_interval_min=30),
        last_post_utc=NOW - timedelta(minutes=10),
        now=NOW,
        source_order={"s": 0},
    )
    assert selection.gated
    assert not selection.to_post
    assert not selection.dropped  # nothing is lost while gated
    assert len(selection.deferred) == 1


def test_interval_gate_opens_after_interval():
    selection = select_for_channel(
        [cand("s", 10)],
        make_channel(post_interval_min=30),
        last_post_utc=NOW - timedelta(minutes=31),
        now=NOW,
        source_order={"s": 0},
    )
    assert not selection.gated
    assert len(selection.to_post) == 1
