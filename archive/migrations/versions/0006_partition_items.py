"""Daily partitions on items and its dependants; raw_xml eviction columns.

A 7-day retention window needs daily granularity — weekly partitions cannot
drop a day. Each dependant is partitioned on the *same* boundary so a day drops
as a unit, which is the only way to drop anything at all while foreign keys
point into items.

**This migration pauses ingest.** It holds the fetch advisory lock across a
full copy of items, because create-new/INSERT-SELECT/rename silently loses any
row written during the copy. Expect a few minutes at 4.4 GB; the phase report
carries the measured number.

Its downgrade() refuses. See the docstring there.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07
"""

import logging

from alembic import op

from archive.migration_support import (
    CATCH_UP_MARGIN,
    lift_timeouts,
    set_lock_timeout,
    take_fetch_lock,
)
from archive.partitions import PARTITION_LOOKAHEAD_DAYS, partition_ddl

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# Every dependant gets a copy of items.first_seen_utc rather than its own
# clock-derived column. That is not a style choice: putting now() into
# deliveries' primary key would break the post-once guarantee, because a
# retried claim evaluates a fresh now(), misses the conflict target, and posts
# the same item to the same channel twice. A column that is a function of
# item_id cannot do that — the three-column conflict target stays exactly
# equivalent to the old two-column one. One name across all three also lets the
# partition-drop loop treat them uniformly.
PARTITION_COLUMN = "first_seen_utc"

NEW_ITEMS_DDL = """
CREATE TABLE items_partitioned (
  item_id          uuid NOT NULL,
  source_name      text NOT NULL,
  fetch_id         uuid NOT NULL,
  source_item_id   text NOT NULL,
  raw_xml          bytea,
  raw_blob_path    text,
  raw_blob_offset  integer,
  raw_blob_length  integer,
  title            text NOT NULL,
  lead_original    text,
  lead_language    text,
  url_raw          text NOT NULL,
  canonical_url    text NOT NULL,
  image_url        text,
  published_raw    text,
  published_utc    timestamptz,
  first_seen_utc   timestamptz NOT NULL,
  content_hash     bytea NOT NULL,
  title_simhash    bigint NOT NULL,
  duplicate_of     uuid,
  copyright_holder text,
  spec_hash        text NOT NULL REFERENCES spec_versions(spec_hash),
  PRIMARY KEY (item_id, first_seen_utc)
) PARTITION BY RANGE (first_seen_utc);
"""

# Secondary indexes are built *after* the swap: the names collide with the old
# tables' until those are dropped, and bulk-load-then-index beats maintaining
# them through the copy.
#
# items had 7 indexes. Deliberately NOT recreated, each with the reason:
#
#   UNIQUE (source_name, source_item_id)
#       Illegal on a partitioned table without the partition key, and adding
#       it would silently permit the same article in a later partition. The
#       guarantee moved to item_keys in 0005.
#   (canonical_url), (content_hash)
#       Served by item_keys, which is where every dedupe lookup goes now.
#   (published_utc DESC)
#       No query orders or filters by it; cli export orders by first_seen_utc.
#
# Kept, each justified by a query that exists:
POST_SWAP_INDEXES = """
-- writer.get_postable_backlog joins items and orders by first_seen_utc; also
-- the range scan behind every archive export and every partition-bounded read.
CREATE INDEX items_first_seen_idx ON items (first_seen_utc DESC);
-- stats.source_stats groups per source over a time range.
CREATE INDEX items_source_first_seen_idx ON items (source_name, first_seen_utc DESC);
-- router category filtering reads these by value.
CREATE INDEX item_categories_category_idx ON item_categories (category);
-- stats.channel_stats and the backlog query.
CREATE INDEX routing_decisions_channel_idx
  ON routing_decisions (channel_name, decision, decided_utc DESC);
-- stats.delivery_stats.
CREATE INDEX deliveries_channel_idx ON deliveries (channel_name, posted_utc DESC);

-- Postgres does not rename indexes with their table.
ALTER INDEX items_partitioned_pkey             RENAME TO items_pkey;
ALTER INDEX item_categories_partitioned_pkey   RENAME TO item_categories_pkey;
ALTER INDEX routing_decisions_partitioned_pkey RENAME TO routing_decisions_pkey;
ALTER INDEX deliveries_partitioned_pkey        RENAME TO deliveries_pkey;
"""

DEPENDANTS = {
    "item_categories": """
        CREATE TABLE item_categories_partitioned (
          item_id        uuid NOT NULL,
          first_seen_utc timestamptz NOT NULL,
          ordinal        integer NOT NULL,
          category       text NOT NULL,
          PRIMARY KEY (item_id, first_seen_utc, ordinal)
        ) PARTITION BY RANGE (first_seen_utc);
    """,
    "routing_decisions": """
        CREATE TABLE routing_decisions_partitioned (
          item_id        uuid NOT NULL,
          channel_name   text NOT NULL,
          first_seen_utc timestamptz NOT NULL,
          decision       routing_decision_t NOT NULL,
          reason         text,
          matched_clause jsonb,
          -- Real data, and deliberately NOT the partition key: it records when
          -- the decision was taken, which a re-decision changes.
          decided_utc    timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (item_id, channel_name, first_seen_utc)
        ) PARTITION BY RANGE (first_seen_utc);
    """,
    "deliveries": """
        CREATE TABLE deliveries_partitioned (
          item_id             uuid NOT NULL,
          channel_name        text NOT NULL,
          first_seen_utc      timestamptz NOT NULL,
          chat_id             text NOT NULL,
          telegram_message_id bigint,
          -- Nullable, so it cannot be a range partition key even though it is
          -- the more natural date for this table.
          posted_utc          timestamptz,
          status              delivery_status NOT NULL,
          attempt             integer NOT NULL DEFAULT 1,
          error               text,
          PRIMARY KEY (item_id, channel_name, first_seen_utc)
        ) PARTITION BY RANGE (first_seen_utc);
    """,
}

COPY_SQL = {
    "items": """
        INSERT INTO items_partitioned (
          item_id, source_name, fetch_id, source_item_id, raw_xml, title,
          lead_original, lead_language, url_raw, canonical_url, image_url,
          published_raw, published_utc, first_seen_utc, content_hash,
          title_simhash, duplicate_of, copyright_holder, spec_hash)
        SELECT item_id, source_name, fetch_id, source_item_id, raw_xml, title,
               lead_original, lead_language, url_raw, canonical_url, image_url,
               published_raw, published_utc, first_seen_utc, content_hash,
               title_simhash, duplicate_of, copyright_holder, spec_hash
        FROM items {where}
        ON CONFLICT DO NOTHING
    """,
    "item_categories": """
        INSERT INTO item_categories_partitioned
          (item_id, first_seen_utc, ordinal, category)
        SELECT c.item_id, i.first_seen_utc, c.ordinal, c.category
        FROM item_categories c JOIN items i USING (item_id) {where}
        ON CONFLICT DO NOTHING
    """,
    "routing_decisions": """
        INSERT INTO routing_decisions_partitioned
          (item_id, channel_name, first_seen_utc, decision, reason,
           matched_clause, decided_utc)
        SELECT r.item_id, r.channel_name, i.first_seen_utc, r.decision, r.reason,
               r.matched_clause, r.decided_utc
        FROM routing_decisions r JOIN items i USING (item_id) {where}
        ON CONFLICT DO NOTHING
    """,
    "deliveries": """
        INSERT INTO deliveries_partitioned
          (item_id, channel_name, first_seen_utc, chat_id, telegram_message_id,
           posted_utc, status, attempt, error)
        SELECT d.item_id, d.channel_name, i.first_seen_utc, d.chat_id,
               d.telegram_message_id, d.posted_utc, d.status, d.attempt, d.error
        FROM deliveries d JOIN items i USING (item_id) {where}
        ON CONFLICT DO NOTHING
    """,
}

SWAP_SQL = """
-- error_events.item_id: FK dropped, column kept. An error event lives 365 days
-- and must outlive the item it points at; the FK would block every partition
-- drop (hazard 2).
ALTER TABLE error_events DROP CONSTRAINT IF EXISTS error_events_item_id_fkey;

DROP TABLE deliveries, routing_decisions, item_categories, items CASCADE;

ALTER TABLE items_partitioned             RENAME TO items;
ALTER TABLE item_categories_partitioned   RENAME TO item_categories;
ALTER TABLE routing_decisions_partitioned RENAME TO routing_decisions;
ALTER TABLE deliveries_partitioned        RENAME TO deliveries;
"""


def _tables():
    return ["items", "item_categories", "routing_decisions", "deliveries"]


def upgrade() -> None:
    lift_timeouts()
    take_fetch_lock()

    bind = op.get_bind()
    op.execute(NEW_ITEMS_DDL)
    for ddl in DEPENDANTS.values():
        op.execute(ddl)

    # Partitions must exist before any row lands. Cover the full history plus
    # the lookahead, or the copy fails on the first row with no home.
    bounds = bind.exec_driver_sql(
        "SELECT min(first_seen_utc)::date AS lo, max(first_seen_utc)::date AS hi FROM items"
    ).fetchone()
    for statement in partition_ddl(bounds.lo, bounds.hi, lookahead=PARTITION_LOOKAHEAD_DAYS,
                                    parent_suffix="_partitioned"):
        bind.exec_driver_sql(statement)

    snapshot = bind.exec_driver_sql("SELECT now()").scalar()
    for table in _tables():
        bind.exec_driver_sql(COPY_SQL[table].format(where=""))

    # Anything the fetch job wrote between the snapshot and now. The margin is
    # deliberate: first_seen_utc is minted client-side from the UUIDv7 while
    # the snapshot comes from the server, and the pass is idempotent, so
    # over-scanning costs I/O while under-scanning costs rows.
    alias = {"items": "", "item_categories": "i.", "routing_decisions": "i.", "deliveries": "i."}
    for table in _tables():
        where = (
            f"WHERE {alias[table]}first_seen_utc > "
            f"'{snapshot.isoformat()}'::timestamptz - interval '{CATCH_UP_MARGIN}'"
        )
        bind.exec_driver_sql(COPY_SQL[table].format(where=where))

    # Never rename on a mismatch. A lost row here is invisible afterwards.
    for table in _tables():
        before = bind.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
        after = bind.exec_driver_sql(f"SELECT count(*) FROM {table}_partitioned").scalar()
        if before != after:
            raise RuntimeError(
                f"{table}: {before} rows before, {after} copied — aborting before "
                "the rename rather than losing the difference"
            )

    # The only ACCESS EXCLUSIVE section. lock_timeout so it fails fast instead
    # of queueing ahead of every query that arrives behind it.
    set_lock_timeout("5s")
    op.execute(SWAP_SQL)
    op.execute(POST_SWAP_INDEXES)
    log.info("0006: partitioned %s", ", ".join(_tables()))


def downgrade() -> None:
    """Refuses, deliberately.

    Un-partitioning is mechanical, but by the time anyone wants to roll back,
    the archive job has dropped partitions whose only remaining copy is
    Parquet in object storage. No downgrade can restore those days, and one
    that silently returned a subset — or rows with raw_xml NULL — would be
    worse than one that refuses.

    A downgrade that works only in the window before the first archive run is
    a trap, not a safety net. Roll back with a restore instead: the server
    keeps 7 days of point-in-time backups, and the procedure is in
    docs/infrastructure-runbook.md.
    """
    raise NotImplementedError(
        "0006 cannot be downgraded: partitions dropped by the archive job exist "
        "only in object storage. Restore from backup — see "
        "docs/infrastructure-runbook.md."
    )
