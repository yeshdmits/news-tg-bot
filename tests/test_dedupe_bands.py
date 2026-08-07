"""LSH banding: the recall guarantee, and the invariant it rests on.

Banding replaced a linear scan that shipped every in-window original to the
client — 30 ms against 27k rows, extrapolating to ~1.5 s per ingested item once
a 72 h window holds 1.5 M. The replacement is only worth having if it finds
everything the scan would have found.
"""

from __future__ import annotations

import random

import pytest

from archive.dedupe import (
    NEAR_DUP_BANDS,
    NEAR_DUP_MAX_HAMMING,
    hamming,
    simhash64,
    simhash_bands,
)


def test_the_pigeonhole_invariant_holds_for_the_configured_values():
    """Full recall requires more bands than the distance threshold. This is
    asserted at import time in archive.dedupe; the test states why."""
    assert NEAR_DUP_BANDS > NEAR_DUP_MAX_HAMMING


def test_bands_cover_the_whole_hash_without_overlapping():
    value = 0x0123_4567_89AB_CDEF
    bands = simhash_bands(value)
    assert len(bands) == NEAR_DUP_BANDS
    # Reassembling the chunks must reproduce the original 64 bits exactly: a
    # gap would mean bits nothing indexes, an overlap would break pigeonhole.
    rebuilt = 0
    for i, band in enumerate(bands):
        rebuilt |= ((band + (1 << 15)) & 0xFFFF) << (i * 16)
    assert rebuilt == value


@pytest.mark.parametrize("value", [0, -1, 1, (1 << 63) - 1, -(1 << 63)])
def test_bands_fit_postgres_smallint_for_extreme_hashes(value):
    """The columns are int2 to halve the storage; an out-of-range value would
    fail the insert at ingest time, on production only."""
    for band in simhash_bands(value):
        assert -32768 <= band <= 32767


def test_bands_are_sign_agnostic():
    """title_simhash is stored signed; the same bits must produce the same
    bands whichever way the value arrived."""
    positive = 0x7FFF_FFFF_FFFF_FFFF
    assert simhash_bands(positive) == simhash_bands(positive - (1 << 64))


def test_every_pair_within_the_threshold_shares_a_band():
    """The recall guarantee, exhaustively over the bit positions that matter.

    If this fails, near-duplicate detection silently stops finding things —
    there is no error, duplicates just start flowing.
    """
    rng = random.Random(99)
    for _ in range(300):
        base = rng.getrandbits(64)
        for distance in range(1, NEAR_DUP_MAX_HAMMING + 1):
            flipped = base
            for bit in rng.sample(range(64), distance):
                flipped ^= 1 << bit
            assert hamming(base, flipped) == distance
            shared = set(enumerate(simhash_bands(base))) & set(enumerate(simhash_bands(flipped)))
            assert shared, (
                f"distance {distance} left no band intact with "
                f"{NEAR_DUP_BANDS} bands — recall is not 100%"
            )


def test_one_bit_beyond_the_threshold_may_miss():
    """Documents the boundary rather than implying the scheme is exact at any
    distance: with n bands, d = n can differ in every band."""
    rng = random.Random(7)
    misses = 0
    for _ in range(500):
        base = rng.getrandbits(64)
        flipped = base
        # One bit in each band — the pigeonhole runs out at exactly d = n.
        for i in range(NEAR_DUP_BANDS):
            flipped ^= 1 << (i * 16 + rng.randrange(16))
        if not set(enumerate(simhash_bands(base))) & set(enumerate(simhash_bands(flipped))):
            misses += 1
    assert misses == 500, "d = NEAR_DUP_BANDS should always miss; the invariant is tight"


def test_python_and_sql_band_extraction_agree(db):
    """The migration computes bands in SQL and the writer computes them in
    Python. If the two ever disagree, backfilled rows become invisible to
    probes issued for freshly ingested ones — a silent, partial dedupe failure.
    """
    titles = [f"Central bank reviews policy item {i}" for i in range(200)]
    rows = [(simhash64(t),) for t in titles]

    with db.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _band_check (title_simhash bigint)")
        cur.executemany("INSERT INTO _band_check VALUES (%s)", rows)
        sql_bands = cur.execute(
            """
            SELECT title_simhash,
                   ((((title_simhash >> 0)  & 65535)::int - 32768)::smallint) b0,
                   ((((title_simhash >> 16) & 65535)::int - 32768)::smallint) b1,
                   ((((title_simhash >> 32) & 65535)::int - 32768)::smallint) b2,
                   ((((title_simhash >> 48) & 65535)::int - 32768)::smallint) b3
            FROM _band_check
            """
        ).fetchall()

    assert len(sql_bands) == len(titles)
    for row in sql_bands:
        assert simhash_bands(row["title_simhash"]) == (row["b0"], row["b1"], row["b2"], row["b3"])
