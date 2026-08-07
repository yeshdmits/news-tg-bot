"""Splitting routing outcomes between rows and counters must not lose any.

The acceptance property for migration 0004: what the bot *does* is unchanged;
only what is persisted changes. So for any sequence of decisions, the retained
rows plus the counters must equal exactly what routing_decisions alone would
have held.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from archive import writer
from archive.ids import timestamp_of, uuid7
from archive.models import Decision
from archive.writer import RETAINED_DECISIONS

ALL_DECISIONS = [d for d in Decision]


def _spec_version(db, spec_hash="a" * 64):
    writer.ensure_spec_version(db, spec_hash, {"version": 2})
    return spec_hash


def _item(db, spec_hash, *, source="src-1", suffix="1"):
    """Insert a minimal item directly; the routing path only needs its id."""
    item_id = uuid7()
    fetch_id = uuid.uuid4()
    db.execute(
        "INSERT INTO fetches (fetch_id, source_name, started_utc, spec_hash) "
        "VALUES (%s, %s, now(), %s)",
        (fetch_id, source, spec_hash),
    )
    db.execute(
        """
        INSERT INTO items (item_id, source_name, fetch_id, source_item_id, raw_xml,
                           title, url_raw, canonical_url, first_seen_utc,
                           content_hash, title_simhash, spec_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (item_id, source, fetch_id, f"{source}:{suffix}", b"<item/>", "t",
         "https://e.org/a", "https://e.org/a", timestamp_of(item_id),
         b"\x00" * 32, 1, spec_hash),
    )
    # Production never writes an item without its key (writer.store_item claims
    # the key first), and the partition-key lookup resolves through item_keys,
    # so a fixture that skipped it would not be exercising the real shape.
    db.execute(
        """
        INSERT INTO item_keys (source_name, source_item_id, item_id, content_hash,
                               title_simhash, band0, band1, band2, band3,
                               canonical_url, first_seen_utc)
        VALUES (%s, %s, %s, %s, 1, 0, 0, 0, 0, 'https://e.org/a', %s)
        """,
        (source, f"{source}:{suffix}", item_id, b"\x00" * 32, timestamp_of(item_id)),
    )
    return item_id


def _persisted_total(db) -> int:
    rows = db.execute("SELECT count(*) AS c FROM routing_decisions").fetchone()["c"]
    counted = db.execute("SELECT COALESCE(sum(n), 0) AS n FROM routing_stats").fetchone()["n"]
    return rows + counted


def test_every_decision_is_persisted_somewhere(db):
    """One decision in, one decision accounted for — row or counter."""
    spec_hash = _spec_version(db)
    for i, decision in enumerate(ALL_DECISIONS):
        item_id = _item(db, spec_hash, suffix=str(i))
        writer.record_routing_decision(db, item_id, f"chan-{i}", decision)

    assert _persisted_total(db) == len(ALL_DECISIONS)


def test_retained_decisions_keep_a_per_item_row(db):
    spec_hash = _spec_version(db)
    for i, name in enumerate(sorted(RETAINED_DECISIONS)):
        item_id = _item(db, spec_hash, suffix=str(i))
        writer.record_routing_decision(db, item_id, "chan", Decision(name))

    kept = db.execute("SELECT decision FROM routing_decisions").fetchall()
    assert {r["decision"] for r in kept} == RETAINED_DECISIONS
    assert db.execute("SELECT count(*) AS c FROM routing_stats").fetchone()["c"] == 0


def test_negative_decisions_become_counters_only(db):
    spec_hash = _spec_version(db)
    negative = [d for d in ALL_DECISIONS if d.value not in RETAINED_DECISIONS]
    for i, decision in enumerate(negative):
        item_id = _item(db, spec_hash, suffix=str(i))
        writer.record_routing_decision(db, item_id, "chan", decision)

    assert db.execute("SELECT count(*) AS c FROM routing_decisions").fetchone()["c"] == 0
    counted = db.execute("SELECT sum(n) AS n FROM routing_stats").fetchone()["n"]
    assert counted == len(negative)


def test_counters_aggregate_by_day_and_channel_and_decision(db):
    spec_hash = _spec_version(db)
    day = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    for i in range(5):
        item_id = _item(db, spec_hash, suffix=str(i))
        writer.record_routing_decision(
            db, item_id, "chan-a", Decision.FILTERED_CATEGORY, decided_utc=day
        )
    # A different day must not fold into the same counter.
    item_id = _item(db, spec_hash, suffix="later")
    writer.record_routing_decision(
        db, item_id, "chan-a", Decision.FILTERED_CATEGORY,
        decided_utc=day + timedelta(days=1),
    )

    rows = db.execute(
        "SELECT day, n FROM routing_stats WHERE channel_name = 'chan-a' ORDER BY day"
    ).fetchall()
    assert [(r["day"].isoformat(), r["n"]) for r in rows] == [
        ("2026-08-07", 5),
        ("2026-08-08", 1),
    ]


def test_retained_row_reprocessed_stays_a_single_row(db):
    """Crash-retried fetch cycles re-decide the same (item, channel)."""
    spec_hash = _spec_version(db)
    item_id = _item(db, spec_hash)
    for _ in range(3):
        writer.record_routing_decision(db, item_id, "chan", Decision.ROUTED)

    assert db.execute("SELECT count(*) AS c FROM routing_decisions").fetchone()["c"] == 1


def test_aging_out_a_gated_row_moves_it_to_the_counters(db):
    """rate_limited is retained, too_old is not. Updating in place would leave
    a non-retained outcome sitting in routing_decisions forever."""
    spec_hash = _spec_version(db)
    item_id = _item(db, spec_hash)
    writer.record_routing_decision(db, item_id, "chan", Decision.RATE_LIMITED, "gated: x")
    assert db.execute("SELECT count(*) AS c FROM routing_decisions").fetchone()["c"] == 1

    writer.update_routing_decision(db, item_id, "chan", Decision.TOO_OLD, "aged out")

    assert db.execute("SELECT count(*) AS c FROM routing_decisions").fetchone()["c"] == 0
    counted = db.execute(
        "SELECT n FROM routing_stats WHERE decision = 'too_old'"
    ).fetchone()
    assert counted["n"] == 1
    # Still exactly one decision accounted for, not two.
    assert _persisted_total(db) == 1


def test_retiring_an_already_retired_row_does_not_double_count(db):
    """post_phase can pass over the same backlog entry again after a crash."""
    spec_hash = _spec_version(db)
    item_id = _item(db, spec_hash)
    writer.record_routing_decision(db, item_id, "chan", Decision.RATE_LIMITED, "gated: x")

    for _ in range(3):
        writer.update_routing_decision(db, item_id, "chan", Decision.CHANNEL_DISABLED, "gone")

    assert _persisted_total(db) == 1


def test_gated_row_that_posts_is_restored_to_routed(db):
    """mark_posted_after_gate stays a plain update: both states are retained."""
    spec_hash = _spec_version(db)
    item_id = _item(db, spec_hash)
    writer.record_routing_decision(db, item_id, "chan", Decision.RATE_LIMITED, "gated: x")

    writer.mark_posted_after_gate(db, item_id, "chan")

    row = db.execute("SELECT decision, reason FROM routing_decisions").fetchone()
    assert row["decision"] == "routed"
    assert row["reason"] is None
    assert _persisted_total(db) == 1


@pytest.mark.parametrize("decision", ALL_DECISIONS, ids=lambda d: d.value)
def test_reconciliation_holds_for_every_decision_value(db, decision):
    """The whole point: for any outcome, one decision in means one out."""
    spec_hash = _spec_version(db)
    item_id = _item(db, spec_hash)
    writer.record_routing_decision(db, item_id, "chan", decision)
    assert _persisted_total(db) == 1


def test_channel_stats_unions_rows_and_counters(db):
    """cli stats must not under-report the outcomes that became counters —
    which is most of them."""
    from archive.stats import channel_stats

    spec_hash = _spec_version(db)
    for i, decision in enumerate([Decision.ROUTED, Decision.FILTERED_CATEGORY,
                                  Decision.FILTERED_CATEGORY, Decision.TOO_OLD]):
        item_id = _item(db, spec_hash, suffix=str(i))
        writer.record_routing_decision(db, item_id, "chan", decision)

    counts = {r["decision"]: r["n"] for r in channel_stats(db)}

    assert counts == {"routed": 1, "filtered_category": 2, "too_old": 1}
