"""Time-bounded deletion of operational tables.

Every table in the database has an owner in docs/data-model.md's retention
table, including the ones whose answer is "never". On a disk that cannot grow,
a table with no stated policy is a leak waiting to happen.

Scheduling is application-level, not pg_cron. The production server has pg_cron
in shared_preload_libraries but **not** in the azure.extensions allow-list, so
CREATE EXTENSION fails there, and cron.database_name points at `postgres`
rather than the application database. Both are server-parameter changes, so the
same at-most-once-per-window pattern the alert engine already uses
(newsbot/alerts.py) does the job with no infrastructure change. See
docs/infrastructure-runbook.md.

Windows are configurable; none is hardcoded at the call site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import psycopg

log = logging.getLogger(__name__)

# Defaults. Every one of these is overridable through Settings (cli.py).
DEFAULT_SEEN_UPDATES_TTL_HOURS = 24
DEFAULT_FETCHES_TTL_DAYS = 90
# Deliberately longer than ARCHIVE_RETENTION_DAYS: dedupe has to survive the
# purge of the item itself. Past this window a republished old article is
# re-ingested as new — the accepted price of an index that cannot grow
# forever. At 500k items/day, 30 days is already ~15 M rows.
DEFAULT_ITEM_KEYS_TTL_DAYS = 30

# Chunk size for the deletes that can span a lot of rows. The role-level
# idle_in_transaction_session_timeout is 60s (migration 0003), so a single
# unbounded DELETE against a full-size table would be killed mid-flight.
DELETE_CHUNK = 10_000


@dataclass(frozen=True)
class RetentionWindows:
    """Everything the retention pass needs, resolved from config."""

    seen_updates_ttl_hours: int = DEFAULT_SEEN_UPDATES_TTL_HOURS
    fetches_ttl_days: int = DEFAULT_FETCHES_TTL_DAYS
    item_keys_ttl_days: int = DEFAULT_ITEM_KEYS_TTL_DAYS


def purge_seen_updates(conn: psycopg.Connection, *, hours: int) -> int:
    """Delete webhook update_ids older than the window; returns rows removed.

    Unlike the old implementation this counts with a subquery rather than
    RETURNING every deleted id — at volume, materialising the whole deleted set
    just to call len() on it is pure waste.
    """
    return conn.execute(
        "DELETE FROM seen_updates WHERE seen_utc < now() - %s",
        (timedelta(hours=hours),),
    ).rowcount


def purge_fetches(conn: psycopg.Connection, *, days: int) -> int:
    """Age out fetch audit rows.

    **Inert until migration 0006.** `items.fetch_id` still carries a foreign key
    to `fetches` at this point, and `items` still holds every row ever ingested,
    so any delete inside the window would fail on rows that are still
    referenced. 0006 drops that FK (the check is also a random read on every
    insert, which is its own reason to go); until then this returns 0 rather
    than raising, and the ordering dependency is recorded in
    docs/data-model.md's retention table.

    Deleting in chunks so the transaction stays well inside
    idle_in_transaction_session_timeout.
    """
    if _fetch_id_fk_exists(conn):
        return 0

    total = 0
    while True:
        removed = conn.execute(
            """
            DELETE FROM fetches
            WHERE ctid IN (
              SELECT ctid FROM fetches
              WHERE started_utc < now() - %s
              LIMIT %s
            )
            """,
            (timedelta(days=days), DELETE_CHUNK),
        ).rowcount
        total += removed
        if removed < DELETE_CHUNK:
            return total


def _fetch_id_fk_exists(conn: psycopg.Connection) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM pg_constraint
            WHERE conname = 'items_fetch_id_fkey' AND contype = 'f'
            """
        ).fetchone()
        is not None
    )


def purge_item_keys(conn: psycopg.Connection, *, days: int) -> int:
    """Age out dedupe keys.

    Chunked, and each chunk is its own statement rather than one long
    transaction: at 500k items/day a 30-day window holds ~15 M rows, and a
    single unbounded DELETE would sit far past the role's 60 s
    idle_in_transaction_session_timeout and be killed with nothing committed.

    Deleting a key means forgetting that the article was ever seen. Past this
    window a source republishing it will ingest it as new — documented in
    docs/data-model.md, and the reason the window is longer than the item
    retention window rather than equal to it.
    """
    total = 0
    while True:
        removed = conn.execute(
            """
            DELETE FROM item_keys
            WHERE ctid IN (
              SELECT ctid FROM item_keys WHERE first_seen_utc < now() - %s LIMIT %s
            )
            """,
            (timedelta(days=days), DELETE_CHUNK),
        ).rowcount
        total += removed
        if removed < DELETE_CHUNK:
            return total


def run(conn: psycopg.Connection, windows: RetentionWindows) -> dict[str, int]:
    """Every window-bounded delete this module owns, in one pass."""
    removed = {
        "seen_updates": purge_seen_updates(conn, hours=windows.seen_updates_ttl_hours),
        "fetches": purge_fetches(conn, days=windows.fetches_ttl_days),
        "item_keys": purge_item_keys(conn, days=windows.item_keys_ttl_days),
    }
    if any(removed.values()):
        log.info("retention pass removed %s", removed)
    return removed
