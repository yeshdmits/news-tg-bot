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
    """64-bit simhash over character trigrams of the normalised title,
    returned as a signed value fitting Postgres bigint. Character shingles
    (rather than word tokens) keep a one-word edit within the Hamming ≤ 3
    near-duplicate threshold on short headline-length text."""
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

    Exact content_hash match first; then a linear simhash scan (Hamming ≤ 3)
    over the last 72 h — deliberately no LSH at this volume. Only
    originals (duplicate_of IS NULL) are candidates so chains stay flat.
    """
    since = now - DEDUPE_WINDOW
    row = conn.execute(
        """
        SELECT item_id FROM items
        WHERE content_hash = %s AND first_seen_utc >= %s AND duplicate_of IS NULL
        ORDER BY first_seen_utc ASC LIMIT 1
        """,
        (chash, since),
    ).fetchone()
    if row:
        return row["item_id"]

    for candidate in conn.execute(
        """
        SELECT item_id, title_simhash FROM items
        WHERE first_seen_utc >= %s AND duplicate_of IS NULL
        ORDER BY first_seen_utc ASC
        """,
        (since,),
    ):
        if hamming(simhash, candidate["title_simhash"]) <= NEAR_DUP_MAX_HAMMING:
            return candidate["item_id"]
    return None
