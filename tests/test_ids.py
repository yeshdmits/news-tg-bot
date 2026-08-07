"""UUIDv7 generation and archive-date derivation.

The join-readiness property this protects — a label carrying only an item_id
finds its item in the archive — is the one thing in the scaling work that
cannot be repaired after the fact, so the midnight boundary and the v4
fallback are both pinned here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from archive.ids import (
    archive_dt_candidates,
    item_id_to_dt,
    legacy_dt_candidates,
    timestamp_of,
    uuid7,
)


def _at(moment: datetime) -> uuid.UUID:
    return uuid7(now_ms=int(moment.timestamp() * 1000))


def test_uuid7_has_the_right_version_and_variant():
    generated = uuid7()
    assert generated.version == 7
    # RFC 9562 variant bits are 0b10.
    assert (generated.int >> 62) & 0b11 == 0b10


def test_timestamp_round_trips_to_the_millisecond():
    moment = datetime(2026, 8, 7, 14, 30, 15, 123000, tzinfo=UTC)
    assert timestamp_of(_at(moment)) == moment


def test_ids_are_time_ordered_across_milliseconds():
    """Sequential B-tree inserts are the reason for UUIDv7 over UUIDv4."""
    base = datetime(2026, 8, 7, tzinfo=UTC)
    ids = [_at(base + timedelta(milliseconds=i)) for i in range(200)]
    assert ids == sorted(ids)


def test_ids_are_monotonic_within_one_millisecond():
    """Without the rand_a counter, a burst inside one millisecond would sort
    randomly and scatter index inserts — the thing UUIDv7 exists to avoid."""
    fixed = int(datetime(2026, 8, 7, tzinfo=UTC).timestamp() * 1000)
    ids = [uuid7(now_ms=fixed) for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_counter_overflow_advances_the_timestamp_rather_than_inverting_order():
    fixed = int(datetime(2026, 8, 7, tzinfo=UTC).timestamp() * 1000)
    ids = [uuid7(now_ms=fixed) for _ in range(5000)]  # > the 4096 counter space
    assert ids == sorted(ids), "sort order broke when rand_a overflowed"
    assert len(set(ids)) == len(ids)


def test_a_real_backwards_clock_step_is_followed_not_clamped():
    """timestamp_of becomes items.first_seen_utc, so an id must never carry a
    time the item was not seen at. Clamping would freeze ids at the pre-step
    timestamp until wall-clock caught up."""
    ahead = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    stepped_back = ahead - timedelta(minutes=5)

    _at(ahead)
    after = _at(stepped_back)

    assert timestamp_of(after) == stepped_back


def test_a_small_backwards_delta_is_treated_as_our_own_overflow_nudge():
    """Counter overflow pushes the issued timestamp ahead of the caller's
    clock; that must not be mistaken for a clock step and reset the counter."""
    fixed = int(datetime(2026, 8, 7, 9, 0, 0, tzinfo=UTC).timestamp() * 1000)
    ids = [uuid7(now_ms=fixed) for _ in range(5000)]
    assert ids == sorted(ids)
    # The burst spilled past one millisecond, but only just.
    span = timestamp_of(ids[-1]) - timestamp_of(ids[0])
    assert timedelta(0) < span < timedelta(seconds=1)


@pytest.mark.parametrize(
    "moment,expected",
    [
        (datetime(2026, 8, 7, 0, 0, 0, tzinfo=UTC), date(2026, 8, 7)),
        (datetime(2026, 8, 7, 23, 59, 59, 999000, tzinfo=UTC), date(2026, 8, 7)),
        (datetime(2026, 8, 8, 0, 0, 0, 1000, tzinfo=UTC), date(2026, 8, 8)),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), date(2026, 12, 31)),
    ],
)
def test_archive_date_at_and_around_midnight(moment, expected):
    assert item_id_to_dt(_at(moment)) == expected


def test_candidates_are_a_single_day_for_uuidv7():
    """first_seen_utc is derived from this same timestamp, so there is no skew
    to absorb — and three candidates would triple point-lookup I/O."""
    assert archive_dt_candidates(_at(datetime(2026, 8, 7, 23, 59, 59, tzinfo=UTC))) == [
        date(2026, 8, 7)
    ]


def test_uuidv4_is_not_derivable():
    legacy = uuid.uuid4()
    assert timestamp_of(legacy) is None
    assert item_id_to_dt(legacy) is None
    # Empty, not a guess: the caller must consult legacy_item_index.
    assert archive_dt_candidates(legacy) == []


def test_legacy_candidates_span_a_midnight_either_side():
    assert legacy_dt_candidates(date(2026, 8, 7)) == [
        date(2026, 8, 7),
        date(2026, 8, 6),
        date(2026, 8, 8),
    ]


def test_generated_ids_are_unique_under_concurrency():
    """The module-level counter is shared, so it is locked."""
    import threading

    seen: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker():
        batch = [uuid7() for _ in range(500)]
        with lock:
            seen.extend(batch)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen)) == len(seen) == 4000
