"""The only module that writes archive tables.

Consumer contract: the fetch/store path writes ``fetches``, ``items``,
``item_categories`` and ``routing_decisions``; ``newsbot`` calls only the
delivery, channel-state and translation helpers. Transaction ownership is the
caller's — wrap calls in ``archive.db.transaction``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from archive.dedupe import canonical_url, content_hash, simhash64
from archive.ids import timestamp_of, uuid7
from archive.models import ChannelState, Decision, DeliveryStatus, ItemRecord, SourceState
from fetcher.parse import RawItem


def ensure_spec_version(conn: psycopg.Connection, spec_hash: str, spec_raw: dict[str, Any]) -> None:
    """Store the spec snapshot under its hash; idempotent."""
    conn.execute(
        """
        INSERT INTO spec_versions (spec_hash, spec) VALUES (%s, %s)
        ON CONFLICT (spec_hash) DO NOTHING
        """,
        (spec_hash, Jsonb(spec_raw)),
    )


def load_source_state(conn: psycopg.Connection, source_name: str, now: datetime) -> SourceState:
    """The source's scheduling row, or a fresh state due immediately when the
    source has never been fetched."""
    row = conn.execute(
        "SELECT * FROM source_state WHERE source_name = %s", (source_name,)
    ).fetchone()
    if row is None:
        return SourceState(source_name=source_name, next_fetch_utc=now)
    return SourceState(**row)


def save_source_state(conn: psycopg.Connection, state: SourceState) -> None:
    """Upsert the source's scheduling row."""
    conn.execute(
        """
        INSERT INTO source_state
          (source_name, next_fetch_utc, last_fetch_utc, etag, last_modified,
           cold_start_done, consecutive_failures)
        VALUES (%(source_name)s, %(next_fetch_utc)s, %(last_fetch_utc)s,
                %(etag)s, %(last_modified)s, %(cold_start_done)s, %(consecutive_failures)s)
        ON CONFLICT (source_name) DO UPDATE SET
          next_fetch_utc = EXCLUDED.next_fetch_utc,
          last_fetch_utc = EXCLUDED.last_fetch_utc,
          etag = EXCLUDED.etag,
          last_modified = EXCLUDED.last_modified,
          cold_start_done = EXCLUDED.cold_start_done,
          consecutive_failures = EXCLUDED.consecutive_failures
        """,
        state.model_dump(),
    )


# Reason prefix that marks a rate_limited decision as gated-not-dropped:
# the item stays in the postable backlog and is retried next window.
GATED_REASON_PREFIX = "gated:"

# Outcomes that keep a per-item row in routing_decisions. Everything else is
# counted in routing_stats instead: at 500k items/day one row per item per
# bound channel makes this the largest table in the system, and the negative
# outcomes are only ever read in aggregate.
#
# The three kept are the ones a person actually investigates per item —
# "why did this post", "why is this stuck", "what was this a duplicate of" —
# and `rate_limited` additionally *must* stay, because the postable backlog is
# a query over it (see get_postable_backlog).
RETAINED_DECISIONS = frozenset({"routed", "rate_limited", "duplicate"})


def try_acquire_post_slot(
    conn: psycopg.Connection, channel_name: str, post_interval_min: int
) -> bool:
    """Atomically claim the channel's posting window. The conditional upsert
    replaces the old read-then-write on channel_state, which would double-post
    under two overlapping job executions: both would read the same
    last_post_utc and both would pass the interval check."""
    row = conn.execute(
        """
        INSERT INTO channel_state (channel_name, last_post_utc)
        VALUES (%s, now())
        ON CONFLICT (channel_name) DO UPDATE SET last_post_utc = now()
        WHERE channel_state.last_post_utc IS NULL
           OR channel_state.last_post_utc <= now() - make_interval(mins => %s)
        RETURNING last_post_utc
        """,
        (channel_name, post_interval_min),
    ).fetchone()
    return row is not None


def load_channel_state(conn: psycopg.Connection, channel_name: str) -> ChannelState:
    """The channel's posting row, or an empty state when none exists yet."""
    row = conn.execute(
        "SELECT * FROM channel_state WHERE channel_name = %s", (channel_name,)
    ).fetchone()
    return ChannelState(**row) if row else ChannelState(channel_name=channel_name)


def save_channel_state(conn: psycopg.Connection, state: ChannelState) -> None:
    """Upsert the channel's posting row."""
    conn.execute(
        """
        INSERT INTO channel_state (channel_name, last_post_utc)
        VALUES (%(channel_name)s, %(last_post_utc)s)
        ON CONFLICT (channel_name) DO UPDATE SET last_post_utc = EXCLUDED.last_post_utc
        """,
        state.model_dump(),
    )


def record_fetch(
    conn: psycopg.Connection,
    *,
    source_name: str,
    started_utc: datetime,
    finished_utc: datetime | None,
    http_status: int | None,
    etag: str | None,
    last_modified: str | None,
    item_count: int,
    new_item_count: int,
    possible_gap: bool,
    spec_hash: str,
    error: str | None = None,
) -> UUID:
    """Insert one fetch-attempt audit row; returns the new fetch_id."""
    row = conn.execute(
        """
        INSERT INTO fetches
          (source_name, started_utc, finished_utc, http_status, etag, last_modified,
           item_count, new_item_count, possible_gap, spec_hash, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING fetch_id
        """,
        (source_name, started_utc, finished_utc, http_status, etag, last_modified,
         item_count, new_item_count, possible_gap, spec_hash, error),
    ).fetchone()
    return row["fetch_id"]


def store_item(
    conn: psycopg.Connection,
    fetch_id: UUID,
    item: RawItem,
    spec_hash: str,
    duplicate_of: UUID | None,
) -> tuple[UUID, bool]:
    """Insert an item; idempotent on (source_name, source_item_id).

    Returns (item_id, is_new). Categories are written only for new rows.
    """
    canon = canonical_url(item.url)
    # The id is minted here rather than by a server default, and first_seen_utc
    # is read back out of it. Deriving rather than calling now() means the
    # archive date embedded in the id and the column the partition is keyed on
    # cannot drift apart — which is what lets a future label find this item
    # from its id alone (archive/ids.py). It also removes the transaction-start
    # skew: now() inside the per-fetch transaction is the time the transaction
    # opened, so a fetch spanning midnight used to stamp items with the
    # previous day.
    item_id = uuid7()
    first_seen_utc = timestamp_of(item_id)
    row = conn.execute(
        """
        INSERT INTO items
          (item_id, source_name, fetch_id, source_item_id, raw_xml, title,
           lead_original, lead_language, url_raw, canonical_url, image_url,
           published_raw, published_utc, first_seen_utc, content_hash,
           title_simhash, duplicate_of, copyright_holder, spec_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_name, source_item_id) DO NOTHING
        RETURNING item_id
        """,
        (
            item_id, item.source_name, fetch_id, item.source_item_id, item.raw_xml,
            item.title, item.lead, item.language, item.url, canon,
            item.image_url, item.published_raw, item.published_utc, first_seen_utc,
            content_hash(canon, item.title), simhash64(item.title),
            duplicate_of, item.copyright_holder, spec_hash,
        ),
    ).fetchone()
    if row is None:
        existing = conn.execute(
            "SELECT item_id FROM items WHERE source_name = %s AND source_item_id = %s",
            (item.source_name, item.source_item_id),
        ).fetchone()
        return existing["item_id"], False

    item_id = row["item_id"]
    for ordinal, category in enumerate(item.categories):
        conn.execute(
            "INSERT INTO item_categories (item_id, ordinal, category) VALUES (%s, %s, %s)",
            (item_id, ordinal, category),
        )
    return item_id, True


def record_routing_decision(
    conn: psycopg.Connection,
    item_id: UUID,
    channel_name: str,
    decision: Decision,
    reason: str | None = None,
    matched_clause: dict[str, Any] | None = None,
    decided_utc: datetime | None = None,
) -> None:
    """Record one (item, channel) outcome.

    Outcomes in RETAINED_DECISIONS keep a per-item row; the rest only increment
    a per-day counter. Nothing the bot *does* changes — this is purely what
    gets persisted — but at 500k items/day it is the difference between the
    largest table in the system and a few hundred rows a day.

    Idempotent for the retained set: re-processing an already-decided (item,
    channel) is a no-op, which is what makes crash-retried fetch cycles safe.
    The counters cannot be idempotent — a counter has no per-item identity to
    conflict on — so a retried fetch cycle can double-count a negative outcome.
    That is an accepted, documented inaccuracy in a debugging aggregate; see
    docs/data-model.md.
    """
    if decision.value in RETAINED_DECISIONS:
        conn.execute(
            """
            INSERT INTO routing_decisions
              (item_id, channel_name, decision, reason, matched_clause, decided_utc)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (item_id, channel_name) DO NOTHING
            """,
            (item_id, channel_name, decision.value, reason,
             Jsonb(matched_clause) if matched_clause is not None else None,
             decided_utc),
        )
        return

    conn.execute(
        """
        INSERT INTO routing_stats (day, channel_name, decision, n)
        VALUES (COALESCE(%s, now())::date, %s, %s, 1)
        ON CONFLICT (day, channel_name, decision) DO UPDATE
          SET n = routing_stats.n + 1
        """,
        (decided_utc, channel_name, decision.value),
    )


def update_routing_decision(
    conn: psycopg.Connection,
    item_id: UUID,
    channel_name: str,
    decision: Decision,
    reason: str | None = None,
) -> None:
    """Re-decide an existing (item, channel): the routed → rate_limited
    transition under drop_oldest, the backlog age-out to too_old, and the
    retirement of a binding that left the spec.

    A transition can cross the RETAINED_DECISIONS boundary — a gated
    ``rate_limited`` row aged out to ``too_old`` is the common case — so the
    row moves to the counters instead of being updated in place. Updating it
    would quietly leave non-retained outcomes in routing_decisions and defeat
    the whole point of the split.
    """
    if decision.value in RETAINED_DECISIONS:
        conn.execute(
            """
            UPDATE routing_decisions
            SET decision = %s, reason = %s, decided_utc = now()
            WHERE item_id = %s AND channel_name = %s
            """,
            (decision.value, reason, item_id, channel_name),
        )
        return

    deleted = conn.execute(
        """
        DELETE FROM routing_decisions
        WHERE item_id = %s AND channel_name = %s
        RETURNING decided_utc
        """,
        (item_id, channel_name),
    ).fetchone()
    # Only count it if a row actually moved: without this, a retried post phase
    # would increment the counter on every pass over an already-retired item.
    if deleted is not None:
        conn.execute(
            """
            INSERT INTO routing_stats (day, channel_name, decision, n)
            VALUES (now()::date, %s, %s, 1)
            ON CONFLICT (day, channel_name, decision) DO UPDATE
              SET n = routing_stats.n + 1
            """,
            (channel_name, decision.value),
        )


def mark_posted_after_gate(conn: psycopg.Connection, item_id: UUID, channel_name: str) -> None:
    """A gated (rate_limited) item that actually posted reads 'routed' again,
    so the archive's final word matches what happened."""
    conn.execute(
        """
        UPDATE routing_decisions
        SET decision = 'routed', reason = NULL, decided_utc = now()
        WHERE item_id = %s AND channel_name = %s AND decision = 'rate_limited'
        """,
        (item_id, channel_name),
    )


def record_delivery(
    conn: psycopg.Connection,
    *,
    item_id: UUID,
    channel_name: str,
    chat_id: str,
    status: DeliveryStatus,
    telegram_message_id: int | None = None,
    posted_utc: datetime | None = None,
    attempt: int = 1,
    error: str | None = None,
) -> None:
    """Upsert the delivery outcome for (item, channel); a retry overwrites the
    status and increments the stored attempt counter."""
    conn.execute(
        """
        INSERT INTO deliveries
          (item_id, channel_name, chat_id, telegram_message_id, posted_utc,
           status, attempt, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (item_id, channel_name) DO UPDATE SET
          telegram_message_id = EXCLUDED.telegram_message_id,
          posted_utc = EXCLUDED.posted_utc,
          status = EXCLUDED.status,
          attempt = deliveries.attempt + 1,
          error = EXCLUDED.error
        """,
        (item_id, channel_name, chat_id, telegram_message_id, posted_utc,
         status.value, attempt, error),
    )


_ITEM_COLUMNS = """
    i.item_id, i.source_name, i.source_item_id, i.title, i.lead_original,
    i.lead_language, i.url_raw, i.canonical_url, i.image_url, i.published_raw,
    i.published_utc, i.first_seen_utc, i.content_hash, i.title_simhash,
    i.duplicate_of, i.copyright_holder, i.spec_hash
"""


def _attach_categories(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> list[ItemRecord]:
    records = []
    for row in rows:
        categories = [
            c["category"]
            for c in conn.execute(
                "SELECT category FROM item_categories WHERE item_id = %s ORDER BY ordinal",
                (row["item_id"],),
            )
        ]
        records.append(ItemRecord(**row, categories=tuple(categories)))
    return records


def get_item(conn: psycopg.Connection, item_id: UUID) -> ItemRecord:
    """Load one item with its categories; raises KeyError if absent."""
    row = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM items i WHERE item_id = %s", (item_id,)
    ).fetchone()
    if row is None:
        raise KeyError(item_id)
    return _attach_categories(conn, [row])[0]


MAX_DELIVERY_ATTEMPTS = 3


def get_postable_backlog(conn: psycopg.Connection, channel_name: str) -> list[ItemRecord]:
    """Items routed (or gated by the post window) and still postable — the
    drain_all backlog and the crash-recovery set. Undelivered means no delivery
    row, or a failed one with attempts left; sent/skipped are terminal, as is
    drop_oldest's rate_limited (its reason lacks the gated: prefix)."""
    rows = conn.execute(
        f"""
        SELECT {_ITEM_COLUMNS}
        FROM items i
        JOIN routing_decisions rd ON rd.item_id = i.item_id
        LEFT JOIN deliveries d
          ON d.item_id = i.item_id AND d.channel_name = rd.channel_name
        WHERE rd.channel_name = %s
          AND (rd.decision = 'routed'
               OR (rd.decision = 'rate_limited' AND rd.reason LIKE 'gated:%%'))
          AND (d.item_id IS NULL OR (d.status = 'failed' AND d.attempt < %s))
        ORDER BY i.published_utc ASC NULLS LAST, i.first_seen_utc ASC
        """,
        (channel_name, MAX_DELIVERY_ATTEMPTS),
    ).fetchall()
    return _attach_categories(conn, rows)


def get_cached_translation(
    conn: psycopg.Connection, chash: bytes, field: str, target_language: str
) -> str | None:
    """Cached translation for (content_hash, field, target_language), or None."""
    row = conn.execute(
        """
        SELECT translated FROM translations
        WHERE content_hash = %s AND field = %s AND target_language = %s
        """,
        (chash, field, target_language),
    ).fetchone()
    return row["translated"] if row else None


def store_translation(
    conn: psycopg.Connection,
    *,
    chash: bytes,
    field: str,
    target_language: str,
    translated: str,
    provider: str,
) -> None:
    """Cache a translation; the first write for a given key wins."""
    conn.execute(
        """
        INSERT INTO translations (content_hash, field, target_language, translated, provider)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (content_hash, field, target_language) DO NOTHING
        """,
        (chash, field, target_language, translated, provider),
    )
