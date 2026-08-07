"""item_keys: the dedupe index that outlives the items.

Partitioning `items` in 0006 makes `UNIQUE (source_name, source_item_id)`
illegal — a partitioned table's unique constraints must contain the partition
key, and adding `first_seen_utc` to it would compile, migrate cleanly, and
**silently destroy dedupe**: the same article could be re-ingested into a later
partition. This table is where the uniqueness guarantee goes instead. It is
never partitioned and is purged only by its own TTL, deliberately longer than
the item retention window, so an article republished after its item was dropped
is still recognised.

It also carries LSH bands over `title_simhash`. The old near-duplicate scan
shipped every in-window original to the client: 30 ms against 27k rows,
extrapolating to ~1.5 s per ingested item at a production-sized window. See
archive/dedupe.py for why four bands guarantee full recall at Hamming <= 3.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07
"""

import logging

from alembic import op

from archive.migration_support import CATCH_UP_MARGIN, lift_timeouts, take_fetch_lock

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

SCHEMA_SQL = """
CREATE TABLE item_keys (
  source_name    text NOT NULL,
  source_item_id text NOT NULL,
  item_id        uuid NOT NULL,
  content_hash   bytea NOT NULL,
  title_simhash  bigint NOT NULL,
  -- The 64-bit simhash split into four disjoint 16-bit chunks, each indexed.
  -- smallint rather than int4: 8 bytes per row instead of 16, which at a
  -- 30-day TTL and 500k items/day is ~1.8 GB of difference.
  band0          smallint NOT NULL,
  band1          smallint NOT NULL,
  band2          smallint NOT NULL,
  band3          smallint NOT NULL,
  canonical_url  text NOT NULL,
  -- Not in the brief's schema, added deliberately: the near-duplicate probe
  -- must exclude duplicates as candidates so chains stay flat, and once items
  -- is partitioned this table is the only permanent record of what an item was
  -- a duplicate *of* (hazard 3 — items.duplicate_of may point at a row that
  -- has already been archived and dropped).
  duplicate_of   uuid,
  published_utc  timestamptz,
  first_seen_utc timestamptz NOT NULL,
  archive_dt     date,        -- NULL until the item's day has been archived
  PRIMARY KEY (source_name, source_item_id)
);

CREATE INDEX item_keys_content_hash_idx ON item_keys (content_hash);  -- exact dedupe
CREATE INDEX item_keys_first_seen_idx   ON item_keys (first_seen_utc); -- the TTL purge
CREATE INDEX item_keys_item_id_idx      ON item_keys (item_id);        -- archive_dt updates

-- Deliberately NOT indexed: canonical_url. The brief's schema had one, but no
-- query in the codebase looks a key up by URL — dedupe goes through
-- content_hash, which already incorporates the canonical URL. Measured on 100k
-- rows it was the single largest index at 143 B/row, which at a 30-day TTL and
-- 500k items/day is ~2.1 GB spent on nothing. The column stays: it is part of
-- the tombstone record and costs only heap. Add the index back with a query
-- that needs it, not before.

CREATE INDEX item_keys_band0_idx ON item_keys (band0);
CREATE INDEX item_keys_band1_idx ON item_keys (band1);
CREATE INDEX item_keys_band2_idx ON item_keys (band2);
CREATE INDEX item_keys_band3_idx ON item_keys (band3);
"""

# Band extraction, identical to archive.dedupe.simhash_bands. Masking after the
# shift makes the sign of the stored bigint irrelevant; the -32768 offset moves
# an unsigned 16-bit chunk into smallint's signed range.
# tests/test_dedupe_bands.py asserts SQL and Python agree on real data.
_BAND = "((((title_simhash >> {shift}) & 65535)::int - 32768)::smallint)"
BANDS = ", ".join(_BAND.format(shift=i * 16) for i in range(4))

BACKFILL_SQL = f"""
INSERT INTO item_keys (source_name, source_item_id, item_id, content_hash,
                       title_simhash, band0, band1, band2, band3,
                       canonical_url, duplicate_of, published_utc, first_seen_utc)
SELECT source_name, source_item_id, item_id, content_hash,
       title_simhash, {BANDS},
       canonical_url, duplicate_of, published_utc, first_seen_utc
FROM items
ON CONFLICT (source_name, source_item_id) DO NOTHING
"""

CATCH_UP_SQL = f"""
INSERT INTO item_keys (source_name, source_item_id, item_id, content_hash,
                       title_simhash, band0, band1, band2, band3,
                       canonical_url, duplicate_of, published_utc, first_seen_utc)
SELECT source_name, source_item_id, item_id, content_hash,
       title_simhash, {BANDS},
       canonical_url, duplicate_of, published_utc, first_seen_utc
FROM items
WHERE first_seen_utc > %(snapshot)s - interval '{CATCH_UP_MARGIN}'
ON CONFLICT (source_name, source_item_id) DO NOTHING
"""


def upgrade() -> None:
    lift_timeouts()
    take_fetch_lock()

    bind = op.get_bind()
    op.execute(SCHEMA_SQL)

    snapshot = bind.exec_driver_sql("SELECT now()").scalar()
    bind.exec_driver_sql(BACKFILL_SQL)
    # Second line of defence: anything written between the snapshot and here.
    bind.exec_driver_sql(CATCH_UP_SQL, {"snapshot": snapshot})

    # Every item must have a key. A gap here is a permanent dedupe hole: once
    # 0006 drops items' own UNIQUE (source_name, source_item_id), nothing else
    # would reject the article when its source republishes it, and it would
    # re-ingest as new months later looking like a feed problem.
    missing = bind.exec_driver_sql(
        """
        SELECT count(*) FROM items i
        WHERE NOT EXISTS (
          SELECT 1 FROM item_keys k
          WHERE k.source_name = i.source_name AND k.source_item_id = i.source_item_id
        )
        """
    ).scalar()
    if missing:
        raise RuntimeError(
            f"{missing} items have no item_keys row — aborting rather than "
            "leaving permanent dedupe holes"
        )

    total = bind.exec_driver_sql("SELECT count(*) FROM item_keys").scalar()
    log.info("0005: %d item_keys backfilled", total)


def downgrade() -> None:
    """Clean: item_keys is derived entirely from items, so dropping it loses
    nothing that cannot be rebuilt by re-running upgrade()."""
    lift_timeouts()
    op.execute("DROP TABLE IF EXISTS item_keys")
