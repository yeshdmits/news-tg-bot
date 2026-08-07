# ADR 0010 — A tiered archive: hot window in Postgres, permanent corpus in object storage

## Status

Accepted. Supersedes [ADR 0001](0001-keep-everything-archive.md).

## Context

ADR 0001 decided to keep everything in PostgreSQL forever, and was explicit
that unbounded growth was deliberate: "This will look like a missing-pruning
bug to newcomers; it is not." That held while the bot watched a handful of
central-bank feeds.

It stops holding at 500,000 items/day. The server's storage is 32 GiB with
**auto-grow deliberately disabled**, which turns retention window and ingest
rate into one variable instead of two. Measured on 100k synthetic items, a
single unbounded tier consumes roughly 1 GB/day once indexes are counted — the
disk fills in under a month. Two of the consequences are worse than "the disk
gets full":

- **A full disk makes the server read-only**, so ingestion stops dead rather
  than degrading.
- `item_keys`, the dedupe index that has to outlive the items themselves,
  accumulates ~22 GB/year on its own and would fill the disk unaided.

Deleting old content outright was never an option: the archive's purpose is to
be a permanent, re-parseable record, and it is now also the training corpus for
a personal relevance model.

## Decision

Split the archive into two tiers, and keep everything in one of them.

- **Hot — PostgreSQL, `ARCHIVE_RETENTION` days (default 7).** Daily
  partitions. Everything the running bot needs: dedupe, routing, the postable
  backlog, recent history.
- **Cold — Parquet in object storage, permanent.** One day per `dt=`
  partition, sorted by `item_id`, zstd, `item_categories` flattened into a list
  column, every row carrying its `spec_hash`. Written by a weekly job that
  verifies the readback against a manifest **before** the day's partitions are
  dropped.

Nothing content-related is deleted. The medium changes.

Three properties are required rather than nice to have, because each one is
impossible to add retroactively once the corpus exists:

1. **`item_id` is UUIDv7**, so an item's `dt=` partition is derivable from its
   id alone. A label created at an arbitrary future date, carrying nothing
   else, must still find its item — and any lookup table would need retention
   of its own and would eventually be purged. `first_seen_utc` is stamped from
   the id's timestamp so the two cannot disagree.
2. **Each day's Parquet is sorted by `item_id`**, so row-group statistics turn
   a point lookup into one row group instead of a whole file. The future
   feedback bot does point lookups; the trainer does full scans; sorting serves
   both.
3. **New derived data goes in sidecar datasets**, never by rewriting a past
   day's file. Files already written are immutable.

## Consequences

- The audit trail ADR 0001 promised still answers "why did/didn't this post
  three weeks ago" — but for anything past the hot window it is answered from
  Parquet, not SQL. `cli export` is no longer the only way out of the database;
  it is how the corpus is read.
- **A missed archive run is an outage, not a delay.** Retention is 7 days and
  the job is weekly, so the peak hot window is 14 days by design; a second
  missed run exceeds the storage budget. A daily watchdog checks partition
  count, oldest partition age and free space, and alerts through the existing
  `error_events` / `alert_outbox` path.
- **Dedupe outlives the items.** `item_keys` carries the uniqueness guarantee
  that `UNIQUE (source_name, source_item_id)` cannot once `items` is
  partitioned, on its own longer TTL (default 30 days). Past that TTL a
  republished old article is re-ingested as new — the accepted price of a
  bounded index.
- **The corpus must be readable with no PostgreSQL in existence.** Every row
  carries `spec_hash`, so `spec_versions` is exported alongside it; a dangling
  hash would defeat the column's purpose.
- Rows written before UUIDv7 are not derivable. They are a bounded, one-time
  set recorded in `legacy_item_index`, which is frozen and never grows.
- Delta Lake or Iceberg over the same Parquet files would add row-level deletes
  and time travel. Not adopted — it is a substantial dependency for a
  single-user system — but relevant if content ever has to be removed after the
  fact, which the schema already anticipates by tracking `copyright_holder`.
  Noted in `docs/archive-format.md`, not implemented.

The keep-everything intent of ADR 0001 stands. Only its assumption of a single
unbounded tier does not.
