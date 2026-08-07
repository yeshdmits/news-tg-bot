"""Daily partition maintenance.

Two failure modes matter and neither is subtle once it happens: running out of
partitions stops ingest dead, and a day that will not drop as a unit defeats
the whole retention design. Both are cheap to assert and expensive to discover.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from archive import partitions
from archive.partitions import (
    MINIMUM_LOOKAHEAD_DAYS,
    PARTITION_LOOKAHEAD_DAYS,
    PARTITIONED_TABLES,
    days_of_headroom,
    drop_day,
    ensure_partitions,
    oldest_partition_day,
    partition_name,
)


def test_lookahead_exceeds_the_alerting_minimum():
    """The brief's floor is 7 days; the lookahead has to be comfortably above
    it or every run alerts."""
    assert PARTITION_LOOKAHEAD_DAYS > MINIMUM_LOOKAHEAD_DAYS


def test_every_dependant_is_partitioned_on_the_same_boundary(db):
    """A day can only drop as a unit if all four tables share the boundary."""
    for table in PARTITIONED_TABLES:
        kind = db.execute(
            "SELECT relkind FROM pg_class WHERE relname = %s", (table,)
        ).fetchone()["relkind"]
        assert kind == "p", f"{table} is not partitioned"

        key = db.execute(
            "SELECT pg_get_partkeydef(%s::regclass) AS k", (table,)
        ).fetchone()["k"]
        assert key == "RANGE (first_seen_utc)", f"{table} partitions on {key}"


def test_ensure_partitions_is_idempotent(db):
    first = ensure_partitions(db, lookahead=3)
    second = ensure_partitions(db, lookahead=3)

    assert second == 0, "a second pass created partitions that already existed"
    assert first >= 0


def test_ensure_partitions_creates_the_full_lookahead(db):
    today = datetime.now(UTC).date()
    ensure_partitions(db, lookahead=5, today=today)

    for offset in range(6):
        name = partition_name("items", today + timedelta(days=offset))
        assert db.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS present", (name,)
        ).fetchone()["present"], f"{name} missing"


def test_headroom_counts_consecutive_days_from_today(db):
    today = datetime.now(UTC).date()
    ensure_partitions(db, lookahead=4, today=today)

    assert days_of_headroom(db, today=today) >= 5

    # A gap three days out truncates the headroom there, not at the far end:
    # ingest stops at the first missing day, so that is what must be counted.
    db.execute(f"DROP TABLE {partition_name('items', today + timedelta(days=3))}")
    assert days_of_headroom(db, today=today) == 3


def test_an_insert_past_the_last_partition_fails_loudly(db):
    """There is no default partition, deliberately: routing homeless rows into
    a catch-all would hide the shortfall until the catch-all was itself
    undroppable."""
    import psycopg

    far_future = datetime.now(UTC).date() + timedelta(days=400)
    with pytest.raises(psycopg.errors.CheckViolation, match="no partition of relation"), \
            db.transaction():
            db.execute(
                """
                INSERT INTO items (item_id, source_name, fetch_id, source_item_id,
                                   raw_xml, title, url_raw, canonical_url,
                                   first_seen_utc, content_hash, title_simhash, spec_hash)
                VALUES (gen_random_uuid(), 's', gen_random_uuid(), 'x', '', 't',
                        'u', 'u', %s, '', 1, 'h')
                """,
                (far_future,),
            )


def test_dropping_a_day_removes_it_from_every_table(db):
    today = datetime.now(UTC).date()
    ensure_partitions(db, lookahead=1, today=today)

    dropped = drop_day(db, today)

    assert sorted(dropped) == sorted(partition_name(t, today) for t in PARTITIONED_TABLES)
    for name in dropped:
        assert db.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS present", (name,)
        ).fetchone()["present"] is False


def test_dropping_a_day_that_does_not_exist_is_a_no_op(db):
    """The archive job re-runs after a crash; a second drop must not raise."""
    day = datetime.now(UTC).date() + timedelta(days=90)
    assert drop_day(db, day) == []


def test_oldest_partition_day_finds_the_earliest(db):
    today = datetime.now(UTC).date()
    ensure_partitions(db, lookahead=2, today=today)

    oldest = oldest_partition_day(db)

    assert oldest is not None
    assert oldest <= today


def test_partition_ddl_covers_history_as_well_as_the_future():
    """The repartition migration has to provision every day that already has
    data, or the copy fails on the first row with no home."""
    lo = date(2026, 1, 1)
    hi = date(2026, 1, 3)
    statements = partitions.partition_ddl(lo, hi, lookahead=0, parent_suffix="_partitioned")

    joined = "\n".join(statements)
    assert "items_p20260101" in joined
    assert "PARTITION OF items_partitioned" in joined
    # Every table, every day in the range.
    assert len(statements) >= 3 * len(PARTITIONED_TABLES)
