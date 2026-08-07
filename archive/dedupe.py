"""Dedupe primitives.

Scope is global across sources, window 72 h. Duplicates are stored with
``duplicate_of`` set, never dropped; only the first occurrence is postable.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import psycopg

DEDUPE_WINDOW = timedelta(hours=72)
NEAR_DUP_MAX_HAMMING = 3

# --- LSH banding -----------------------------------------------------------
#
# The near-duplicate scan used to ship every in-window original to the client
# and compare in Python. Measured: 30 ms against 27k rows, which extrapolates
# to ~1.5 s *per ingested item* once a 72 h window holds 1.5 M — about nine
# whole cores for a workload needing 5.8 items/s. Banding turns it into an
# index probe.
#
# Split the 64-bit simhash into NEAR_DUP_BANDS disjoint chunks and index each.
# Two hashes within Hamming distance d differ in at most d bits, so if
# d < NEAR_DUP_BANDS at least one chunk contains none of the differing bits and
# matches exactly. Probing for an exact match on any band therefore returns
# every true near-duplicate — recall is 100%, not approximate.
#
# That guarantee is the pigeonhole principle and it holds **only while
# NEAR_DUP_BANDS > NEAR_DUP_MAX_HAMMING**. Raise the threshold to 5 against 4
# bands and recall degrades silently: duplicates simply start flowing, with no
# error anywhere. Hence the assertion below rather than a comment.
NEAR_DUP_BANDS = 4
_BAND_BITS = 64 // NEAR_DUP_BANDS
_BAND_MASK = (1 << _BAND_BITS) - 1
# Postgres smallint is signed, so a raw 16-bit chunk (0..65535) is shifted into
# range instead of widening the column to int4 — 8 bytes per row rather than
# 16, which at a 30-day item_keys TTL is not a rounding error.
_BAND_OFFSET = 1 << (_BAND_BITS - 1)

assert NEAR_DUP_BANDS > NEAR_DUP_MAX_HAMMING, (
    f"LSH banding guarantees full recall only while NEAR_DUP_BANDS "
    f"({NEAR_DUP_BANDS}) > NEAR_DUP_MAX_HAMMING ({NEAR_DUP_MAX_HAMMING}): with "
    f"{NEAR_DUP_BANDS} disjoint bands, two hashes differing in "
    f"{NEAR_DUP_MAX_HAMMING} bits could differ in every band and be missed. "
    f"Raise NEAR_DUP_BANDS, or lower the threshold — see docs/configuration.md."
)


def simhash_bands(simhash: int) -> tuple[int, ...]:
    """The indexed band values for a simhash, in band order.

    Masking after the shift makes the sign of the stored bigint irrelevant:
    the bits extracted are the same whether the value arrived positive or
    negative. The SQL in migration 0005 computes these identically, and
    tests/test_dedupe_bands.py asserts the two agree.
    """
    return tuple(
        ((simhash >> (i * _BAND_BITS)) & _BAND_MASK) - _BAND_OFFSET
        for i in range(NEAR_DUP_BANDS)
    )


_TRACKING_PARAMS = frozenset({"linktype", "ref", "fbclid"})
_MASK64 = (1 << 64) - 1
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def canonical_url(url: str) -> str:
    """Lowercase host, strip fragment and trailing slash, drop tracking params
    (utm_*, linkType, ref, fbclid)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") if parts.path != "/" else ""
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query_pairs), ""))


def normalise_title(title: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace — the
    canonical title form both dedupe hashes are built on."""
    decomposed = unicodedata.normalize("NFKD", title)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    no_punct = re.sub(r"[^\w\s]+", "", stripped.casefold())
    return re.sub(r"\s+", " ", no_punct).strip()


def content_hash(canon_url: str, title: str) -> bytes:
    """SHA-256 over canonical URL plus normalised title — the exact-duplicate
    key, and the cache key for translations."""
    payload = canon_url.encode() + b"\x00" + normalise_title(title).encode()
    return hashlib.sha256(payload).digest()


def _feature_hash(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest())


def simhash64(title: str) -> int:
    """64-bit simhash over character 3/4/5-grams of the normalised title,
    returned as a signed value fitting Postgres bigint.

    **Measured caveat.** This docstring used to claim that character shingles
    "keep a one-word edit within the Hamming ≤ 3 threshold on short
    headline-length text". They do not: over 30-80 character headlines a
    one-word substitution measures Hamming 4-16 (median 7) and even a single
    substituted character averages ~7. At NEAR_DUP_MAX_HAMMING = 3 the only
    near-duplicates actually detected are *verbatim* republications — the same
    headline run by another outlet, which normalises identically to distance 0.

    That is a real limit on near-duplicate detection, not a bug in this
    function, and it is what the threshold would have to change to address.
    tests/test_seed_tools.py pins the measurement so a later tuning change is
    noticed rather than assumed.
    """
    text = normalise_title(title)
    features = [
        text[i : i + n]
        for n in (3, 4, 5)
        for i in range(max(len(text) - n + 1, 0))
    ] or ([text] if text else [])
    if not features:
        return 0
    votes = [0] * 64
    for feature in features:
        h = _feature_hash(feature)
        for bit in range(64):
            votes[bit] += 1 if (h >> bit) & 1 else -1
    value = sum(1 << bit for bit, vote in enumerate(votes) if vote > 0)
    return value - (1 << 64) if value >= (1 << 63) else value


def hamming(a: int, b: int) -> int:
    """Bit distance between two 64-bit simhashes; signed inputs are masked."""
    return ((a ^ b) & _MASK64).bit_count()


def find_duplicate(
    conn: psycopg.Connection,
    chash: bytes,
    simhash: int,
    now: datetime,
) -> UUID | None:
    """First occurrence to attribute a duplicate to, or None.

    Reads ``item_keys``, not ``items``. Once ``items`` is partitioned and old
    days are dropped, it no longer carries the uniqueness guarantee — and
    ``item_keys`` deliberately outlives it, so an article republished after its
    item was purged is still recognised as a duplicate.

    Exact ``content_hash`` match first, then a banded simhash probe (see
    NEAR_DUP_BANDS). Only originals are candidates, so chains stay flat.
    """
    since = now - DEDUPE_WINDOW
    row = conn.execute(
        """
        SELECT item_id FROM item_keys
        WHERE content_hash = %s AND first_seen_utc >= %s AND duplicate_of IS NULL
        ORDER BY first_seen_utc ASC LIMIT 1
        """,
        (chash, since),
    ).fetchone()
    if row:
        return row["item_id"]

    bands = simhash_bands(simhash)
    # An OR across the four indexed band columns, which the planner turns into
    # a BitmapOr over the four band indexes — verified with EXPLAIN. A
    # UNION ALL rewrite would only pay off if one band value had a very long
    # posting list; measured on 100k seeded items the candidate set per probe
    # is p50 3, p95 14, p99 27, so it does not (docs/data-model.md).
    for candidate in conn.execute(
        """
        SELECT item_id, title_simhash FROM item_keys
        WHERE first_seen_utc >= %(since)s AND duplicate_of IS NULL
          AND (band0 = %(b0)s OR band1 = %(b1)s OR band2 = %(b2)s OR band3 = %(b3)s)
        ORDER BY first_seen_utc ASC
        """,
        {
            "since": since,
            **{f"b{i}": band for i, band in enumerate(bands)},
        },
    ):
        # The band probe is a filter, not the answer: a shared band means the
        # hashes *may* be close. Hamming still decides.
        if hamming(simhash, candidate["title_simhash"]) <= NEAR_DUP_MAX_HAMMING:
            return candidate["item_id"]
    return None
