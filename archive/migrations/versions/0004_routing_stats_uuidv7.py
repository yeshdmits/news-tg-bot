"""Routing volume, UUIDv7 ids, retention windows.

At 500k items/day routing_decisions is the largest table in the system — one
row per item per bound channel, and only about a quarter of those outcomes are
worth a per-item row. The rest become counters in routing_stats.

Item ids become UUIDv7 so an archived item is locatable from its id alone (see
archive/ids.py), and first_seen_utc is stamped from the id's own timestamp
rather than now(), so the two can never disagree. Both server defaults go: with
the writer always supplying a value, a default is a trap for any path that
forgets.

legacy_item_index freezes the pre-UUIDv7 rows, which are the only items whose
archive date is not derivable.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07
"""

import logging

from alembic import op

from archive.migration_support import CATCH_UP_MARGIN, lift_timeouts, take_fetch_lock

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


SCHEMA_SQL = """
-- Per-day, per-channel counters for the routing outcomes that are not worth a
-- row each. Tiny and never purged: a few hundred rows a day at most.
CREATE TABLE routing_stats (
  day          date NOT NULL,
  channel_name text NOT NULL,
  decision     routing_decision_t NOT NULL,
  n            bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (day, channel_name, decision)
);

-- The pre-UUIDv7 items, whose archive date cannot be derived from the id.
-- Frozen by construction: no UUIDv4 item id is ever minted again, so nothing
-- writes here after this migration. It never grows.
CREATE TABLE legacy_item_index (
  item_id    uuid PRIMARY KEY,
  archive_dt date NOT NULL
);
"""

# Roll the non-retained outcomes into counters, then delete them. Both
# statements are bounded on the same snapshot instant, so a concurrent write is
# neither double-counted nor dropped even without the advisory lock.
AGGREGATE_SQL = """
INSERT INTO routing_stats (day, channel_name, decision, n)
SELECT decided_utc::date, channel_name, decision, count(*)
FROM routing_decisions
WHERE decided_utc < %(snapshot)s AND decision <> ALL(%(retained)s)
GROUP BY 1, 2, 3
ON CONFLICT (day, channel_name, decision) DO UPDATE
  SET n = routing_stats.n + EXCLUDED.n
"""

PRUNE_SQL = """
DELETE FROM routing_decisions
WHERE decided_utc < %(snapshot)s AND decision <> ALL(%(retained)s)
"""

# Every item that exists when this runs on the live server is UUIDv4, and its
# archive date is the day it was first seen — the partition it will land in.
#
# The version filter is not decoration. Re-running 0004 after a
# downgrade/upgrade cycle, or in a test, finds UUIDv7 rows too; indexing those
# would contradict the table's whole premise ("only the ids that are not
# derivable") and quietly grow a table documented as frozen. The version nibble
# is the first character of a UUID's third dash-separated group.
NOT_DERIVABLE = "substring(item_id::text, 15, 1) <> '7'"

INDEX_LEGACY_SQL = f"""
INSERT INTO legacy_item_index (item_id, archive_dt)
SELECT item_id, first_seen_utc::date FROM items
WHERE {NOT_DERIVABLE}
ON CONFLICT (item_id) DO NOTHING
"""

CATCH_UP_SQL = f"""
INSERT INTO legacy_item_index (item_id, archive_dt)
SELECT item_id, first_seen_utc::date FROM items
WHERE {NOT_DERIVABLE}
  AND first_seen_utc > %(snapshot)s - interval '{CATCH_UP_MARGIN}'
ON CONFLICT (item_id) DO NOTHING
"""

DEFAULTS_SQL = """
-- Both values now come from the application: item_id from archive.ids.uuid7,
-- and first_seen_utc from that id's embedded timestamp. Leaving the defaults
-- in place would silently paper over any insert path that forgot.
ALTER TABLE items ALTER COLUMN item_id DROP DEFAULT;
ALTER TABLE items ALTER COLUMN first_seen_utc DROP DEFAULT;
"""


def upgrade() -> None:
    from archive.writer import RETAINED_DECISIONS

    lift_timeouts()
    take_fetch_lock()

    bind = op.get_bind()
    op.execute(SCHEMA_SQL)

    params = {
        "snapshot": bind.exec_driver_sql("SELECT now()").scalar(),
        "retained": sorted(RETAINED_DECISIONS),
    }

    bind.exec_driver_sql(AGGREGATE_SQL, params)
    bind.exec_driver_sql(PRUNE_SQL, params)
    bind.exec_driver_sql(INDEX_LEGACY_SQL)
    # Second line of defence: anything written between the snapshot and here.
    bind.exec_driver_sql(CATCH_UP_SQL, params)

    # Every item whose date is not derivable from its id must be indexed, or it
    # becomes permanently unlocatable in the archive — the one failure this
    # whole design exists to prevent, and the one that cannot be repaired later.
    not_derivable, indexed = bind.exec_driver_sql(
        f"SELECT (SELECT count(*) FROM items WHERE {NOT_DERIVABLE}),"
        f"       (SELECT count(*) FROM legacy_item_index)"
    ).fetchone()
    if not_derivable != indexed:
        raise RuntimeError(
            f"legacy_item_index covers {indexed} of {not_derivable} non-derivable items — "
            "aborting rather than leaving items permanently unlocatable in the archive"
        )

    op.execute(DEFAULTS_SQL)
    log.info("0004: %d legacy items indexed, routing_stats backfilled", indexed)


def downgrade() -> None:
    """Schema-complete, but LOSSY.

    The non-retained routing_decisions rows were aggregated into counters and
    deleted; no downgrade can restore them, and routing_stats is dropped with
    everything it knew. tests/test_migrations.py names routing_decisions exempt
    from the round-trip assertion for exactly this reason.
    """
    lift_timeouts()
    op.execute(
        """
        ALTER TABLE items ALTER COLUMN item_id SET DEFAULT gen_random_uuid();
        ALTER TABLE items ALTER COLUMN first_seen_utc SET DEFAULT now();
        DROP TABLE IF EXISTS legacy_item_index;
        DROP TABLE IF EXISTS routing_stats;
        """
    )
