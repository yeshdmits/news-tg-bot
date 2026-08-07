"""Item identifiers, and locating an item in the archive from its id alone.

Items are identified by **UUIDv7** (RFC 9562): a 48-bit big-endian Unix
millisecond timestamp, then the version nibble, then random bits. Two
properties matter here, and both are load-bearing rather than incidental.

**Self-locating.** The archive drops a day's rows from PostgreSQL once they are
safely in Parquet, so a label created a year from now — carrying nothing but an
``item_id`` — must still find its item. Any lookup table would need a retention
policy of its own and would eventually be purged, so the location is instead
*derived* from the id: ``item_id_to_dt`` reads the embedded timestamp and names
the ``dt=`` partition directly. No table, no TTL, no storage cost.

**Time-ordered.** UUIDv7 sorts by creation time, so B-tree inserts land at the
right-hand edge of the index instead of scattering across it the way UUIDv4
does. At 500k items/day that is the difference between appending and rewriting
random pages.

The archive's date comes from ``items.first_seen_utc``, so the two must agree or
the derivation is a guess. They agree by construction: ``timestamp_of`` is what
the writer stamps ``first_seen_utc`` with, rather than the two being read from
independent clocks. That is why ``archive_dt_candidates`` returns a *single*
date for a v7 id — the three-day window that would otherwise be needed to
absorb a midnight boundary is what makes a point lookup read three partitions
instead of one.

Rows created before this scheme are UUIDv4 and carry no timestamp. They are a
bounded, frozen set recorded in ``legacy_item_index`` (see migration 0004), and
for them the ±1 day fallback still applies.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

# RFC 9562 section 5.7. The layout, most significant bit first:
#
#   48 bits  unix_ts_ms      milliseconds since the Unix epoch
#    4 bits  version         0b0111
#   12 bits  rand_a          used here as an intra-millisecond counter
#    2 bits  variant         0b10
#   62 bits  rand_b          random
_VERSION = 7
_UNIX_TS_MS_BITS = 48
_RAND_A_BITS = 12
_MAX_RAND_A = (1 << _RAND_A_BITS) - 1

# How far behind the last issued timestamp the clock may read before we treat
# it as a real backwards step rather than our own counter-overflow nudge.
# Overflow can only push us ahead by (ids in the burst / 4096) ms, so anything
# beyond a second is the clock, not us.
_CLOCK_STEP_TOLERANCE_MS = 1000

_lock = threading.Lock()
_last_ms = -1
_counter = 0


def uuid7_at(moment: datetime, *, entropy: int, counter: int = 0) -> UUID:
    """A UUIDv7 with an explicit timestamp and explicit randomness.

    The layout lives in exactly one place, so a caller that needs reproducible
    ids — tools/seed.py builds a whole corpus from a seeded RNG — does not have
    to restate the bit packing and drift from it.

    ``moment`` is truncated to milliseconds, which is all the format carries.
    Read it back with ``timestamp_of`` rather than reusing the input if the two
    have to agree exactly.
    """
    return _pack(int(moment.timestamp() * 1000), counter & _MAX_RAND_A, entropy)


def _pack(ms: int, counter: int, rand_b: int) -> UUID:
    value = (
        (ms & ((1 << _UNIX_TS_MS_BITS) - 1)) << 80
        | _VERSION << 76
        | counter << 64
        | 0b10 << 62
        | (rand_b & ((1 << 62) - 1))
    )
    return UUID(int=value)


def uuid7(*, now_ms: int | None = None) -> UUID:
    """A new UUIDv7.

    Monotonic within a millisecond: the 12-bit ``rand_a`` field is used as a
    counter so ids minted in the same millisecond still sort in creation order,
    which keeps index inserts sequential under a burst. On counter overflow
    (>4096 ids in one millisecond) the timestamp is nudged forward rather than
    allowing a sort inversion.

    A clock that steps *backwards* by more than ``_CLOCK_STEP_TOLERANCE_MS`` is
    followed, not clamped. Clamping would pin every subsequent id to the old
    timestamp until wall-clock caught up, and since ``timestamp_of`` becomes
    ``items.first_seen_utc`` that would write a time the item was not seen at.
    A brief ordering inversion after an NTP step costs some index locality; a
    lying timestamp costs correctness.

    A *small* backwards delta is not a clock step — it is this function having
    nudged itself ahead on counter overflow — so it stays on the issued
    timestamp and keeps counting.
    """
    global _last_ms, _counter

    with _lock:
        ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        if ms > _last_ms or _last_ms - ms > _CLOCK_STEP_TOLERANCE_MS:
            _last_ms, _counter = ms, 0
        else:
            _counter += 1
            if _counter > _MAX_RAND_A:
                _last_ms += 1
                _counter = 0
            ms = _last_ms
        counter = _counter

    return _pack(ms, counter, int.from_bytes(os.urandom(8)))


def timestamp_of(item_id: UUID) -> datetime | None:
    """The creation time embedded in a UUIDv7, or None for any other version.

    This is the value the writer stamps ``items.first_seen_utc`` with, so the
    column and the id can never disagree.
    """
    if item_id.version != _VERSION:
        return None
    return datetime.fromtimestamp((item_id.int >> 80) / 1000, tz=UTC)


def item_id_to_dt(item_id: UUID) -> date | None:
    """The archive partition date for an item, or None if it is not derivable.

    None means the id predates UUIDv7 and the caller must fall back to
    ``legacy_item_index``.
    """
    moment = timestamp_of(item_id)
    return moment.date() if moment is not None else None


def archive_dt_candidates(item_id: UUID) -> list[date]:
    """Partition dates to search for an item, most likely first.

    One date for a UUIDv7, because ``first_seen_utc`` is derived from the same
    timestamp — reading three partitions per point lookup would triple the I/O
    on the path the feedback bot depends on, to absorb a skew that cannot occur.

    An empty list for anything else: a UUIDv4 id carries no time at all, and
    guessing dates around "now" would be worse than saying so. The caller
    consults ``legacy_item_index``.
    """
    dt = item_id_to_dt(item_id)
    return [dt] if dt is not None else []


def legacy_dt_candidates(archive_dt: date) -> list[date]:
    """Dates to search for a pre-UUIDv7 item, given its ``legacy_item_index``
    date.

    ±1 day, because those rows took ``first_seen_utc`` from ``now()`` at insert
    while the index was backfilled from the same column — a row written either
    side of midnight during the migration can land a day off.
    """
    return [archive_dt, archive_dt - timedelta(days=1), archive_dt + timedelta(days=1)]
