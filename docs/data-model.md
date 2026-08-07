# Data model

One PostgreSQL database, schema owned by alembic
(`archive/migrations/versions/`, plain SQL). The `archive`
package is the **only** code that writes to it.

## Design stance: keep everything, in two tiers

The archive is still append-only for content, and nothing content-related is
deleted. What changed is *where* it lives: PostgreSQL holds a recent hot
window, and everything older moves to Parquet in object storage. The corpus is
permanent; only the medium changes. See
[ADR 0010](adr/0010-tiered-archive.md) — it supersedes ADR 0001, which
specified a single unbounded tier.

The reason is arithmetic. The server's disk is fixed (32 GiB, auto-grow
deliberately off), so retention window and ingest rate stopped being
independent the moment volume grew: at 500k items/day a single tier fills the
disk in weeks, and a full disk makes the server read-only, which stops
ingestion dead.

What each tier is for:

- **Hot (PostgreSQL, `ARCHIVE_RETENTION` days).** Everything the running bot
  needs: dedupe, routing, the postable backlog, recent history.
- **Cold (Parquet in object storage, permanent).** The interpretable record —
  every item, its categories flattened, and the `spec_hash` that produced it.
  Also the training corpus for relevance ranking.

Two things survive independently of both tiers because they answer questions
about items that are no longer in PostgreSQL: `item_keys` (dedupe tombstones,
on their own longer TTL) and `legacy_item_index` (frozen, never grows).

Only operational error data is deleted outright (see
[Retention](#retention)). `cli stats` reports per-source volumes.

## Tables

### Content and provenance (migration 0001)

| Table | One row per | Notes |
|---|---|---|
| `spec_versions` | distinct spec.json content | `spec_hash` (sha256) + the full spec as jsonb. Every fetch, item and error event references the exact spec version that produced it, so the archive stays interpretable after config changes. |
| `fetches` | HTTP fetch of a source | Status, ETag/Last-Modified, item counts, `possible_gap` (feed moved faster than the fetch cadence), error text for failed fetches. |
| `items` | item first seen | `raw_xml` (bytea, NOT NULL), extracted fields, `canonical_url` (tracking params stripped), `content_hash`, `title_simhash`, `duplicate_of` self-reference, `copyright_holder`. Since 0005 the idempotency anchor is `item_keys`, not this table's own `UNIQUE (source_name, source_item_id)` — see [Dedupe](#dedupe-0005). Since 0004, `item_id` is a **UUIDv7** minted by the application and `first_seen_utc` is read out of that id — see [Item identity](#item-identity). |
| `item_categories` | (item, ordinal) | Categories in feed order. |
| `translations` | (content_hash, field, target_language) | Content-keyed translation cache — re-runs and duplicates never re-bill the provider. |
| `routing_decisions` | (item, channel), for retained outcomes only | One decision per item per bound channel, but since 0004 only `routed`, `rate_limited` and `duplicate` keep a row — see [What `routing_decisions` no longer holds](#what-routing_decisions-no-longer-holds). The full outcome set is: `routed`, `filtered_category`, `predicate_failed`, `too_old`, `duplicate`, `channel_disabled`, `cold_start_skip`, or `rate_limited` (with a `reason`; `gated:` reasons are re-evaluated next window, others are terminal). An [archive-only](configuration.md#archive-only-sources) source has no bound channels and so produces **no** rows here — its items are stored with no decision trail at all. `channel_disabled` doubles as the retirement state for a decision whose binding has since disappeared from the spec, carrying the reason `binding removed from spec`. |
| `deliveries` | (item, channel) | `sent` / `failed` / `skipped` with attempt count, Telegram message id, error. Dry-run posts are recorded as `skipped` with error `dry_run`. |

### Operational state (0001)

| Table | Purpose |
|---|---|
| `source_state` | Per-source scheduling: `next_fetch_utc` (the only scheduling state), stored ETag/Last-Modified for conditional GETs, `cold_start_done`, `consecutive_failures` (added in 0002) driving the fetch backoff. |
| `channel_state` | Per-channel `last_post_utc`. The posting window is claimed by a single conditional UPSERT on this table — an atomic claim, so two overlapping executions cannot both post. |

### Error handling (0002)

| Table | Purpose |
|---|---|
| `error_events` | One row per error **fingerprint** (component + class + source/channel + normalized message — ports, digits, uuids and query strings are normalized away so noisy variants group together). Carries severity, state (`unacked`/`acked`/`resolved`), occurrence counters, mute/ack/resolve bookkeeping, and the coordinates of the single ops-chat message the event owns (alerts edit in place rather than spam). |
| `error_occurrences` | Raw occurrences per fingerprint, for forensics. |
| `alert_outbox` | Alerts that could not be sent (Telegram down); flushed at the start of the next execution. |
| `operator_actions` | Audit trail of ops-chat commands (who acked/muted what). |
| `bot_state` | Small key/value store: alert budget window, digest timing, retention bookkeeping, admin cache. |

### Serverless support (0003)

| Table | Purpose |
|---|---|
| `seen_updates` | One row per processed Telegram `update_id`. Webhook idempotency: a redelivered update is acknowledged without re-running its side effects. Rows are purged after 24 h. |

Migration 0003 also sets role-level `statement_timeout = '30s'` and
`idle_in_transaction_session_timeout = '60s'` — a killed serverless container
must not strand a lock or an idle transaction. Migrations run as that role, so
every one that backfills lifts both with `SET LOCAL` for the duration; see
[Writing a migration that backfills](#writing-a-migration-that-backfills).

### Partitioning (0006)

`items`, `item_categories`, `routing_decisions` and `deliveries` are all
`PARTITION BY RANGE (first_seen_utc)` with **daily** partitions — weekly is too
coarse for a 7-day window, since a week cannot drop a day.

Every dependant carries a **copy of `items.first_seen_utc`**, not its own
clock-derived column. That is a correctness requirement, not tidiness: putting
`now()` into `deliveries`' primary key would break the post-once guarantee,
because a retried claim evaluates a fresh timestamp, misses the conflict
target, and posts the same item to the same channel twice. A column that is a
function of `item_id` cannot do that, so the three-column conflict target stays
exactly equivalent to the old two-column one. `routing_decisions.decided_utc`
is left alone — it is real data that a re-decision changes.

**Every lookup on those keys must supply `first_seen_utc`** or the planner
cannot prune. Measured on a 24-partition database, the delivery-claim path goes
from 1 scan node and 187 buffers to 24 and 1229 — and it runs for every routed
item. `archive.writer._item_day` resolves it, from the caller's value on the
hot path and from `item_keys` otherwise.

**There is no default partition.** An insert past the last partition fails
hard, deliberately: a catch-all would hide the shortfall until it was itself
undroppable. `archive/partitions.py` keeps a 14-day lookahead and raises a
`PartitionShortfall` alert below 7.

#### Foreign keys, and why there are none left

A `DROP TABLE partition` fails while any row references it, so every foreign
key into `items` had to go. Each one, with its policy:

| Foreign key | Policy | Why |
|---|---|---|
| `item_categories.item_id` | dropped, cascade-partition | dropped as a unit with the same day |
| `routing_decisions.item_id` | dropped, cascade-partition | same |
| `deliveries.item_id` | dropped, cascade-partition | same |
| `error_events.item_id` | **dropped, column kept** | an event lives 365 days and must outlive the item it points at |
| `items.duplicate_of` (self) | **dropped, column kept** | may point at an already-archived item; `item_keys` is the tombstone |
| `items.fetch_id` | **dropped, column kept** | also removes a random read on every insert, and is what makes `fetches` prunable |
| `items.spec_hash` | **kept** | outbound to `spec_versions`, which is tiny and never purged, so it blocks nothing |

#### The index set

Reduced from 7 to 2 on `items`. Dropped: `UNIQUE (source_name, source_item_id)`
(illegal under partitioning, and the guarantee moved to `item_keys`),
`(canonical_url)` and `(content_hash)` (served by `item_keys`), and
`(published_utc DESC)` (no query uses it). Kept: `(first_seen_utc DESC)` for
the backlog and export range scans, and `(source_name, first_seen_utc DESC)`
for `stats.source_stats`.

### Dedupe (0005)

| Table | Purpose |
|---|---|
| `item_keys` | One row per `(source_name, source_item_id)` ever seen. **The idempotency anchor**, taken over from `items`. Never partitioned; purged only by its own TTL. Carries `content_hash`, `title_simhash` plus its four LSH bands, `canonical_url`, `duplicate_of`, and `archive_dt` (NULL until the item's day is archived). |

`items`' `UNIQUE (source_name, source_item_id)` cannot survive partitioning: a
partitioned table's unique constraints must contain the partition key, and
`UNIQUE (source_name, source_item_id, first_seen_utc)` compiles, migrates
cleanly, and **silently permits the same article in a later partition**. Old
days are also dropped outright, so `ON CONFLICT` against `items` stops firing
for anything already archived. `item_keys` is where the guarantee lives now,
and `writer.store_item` claims the key *before* inserting the item.

**The tradeoff, stated plainly:** past `ITEM_KEYS_TTL_DAYS` a key is deleted
and the article is forgotten. A source republishing it after that window
ingests it as new. That is the price of an index that cannot grow forever — at
500k items/day the table is ~15 M rows at 30 days. The window is deliberately
longer than `ARCHIVE_RETENTION_DAYS` so dedupe survives the purge of the item
itself.

#### Near-duplicate detection

`title_simhash` is split into four disjoint 16-bit bands, each indexed. Two
hashes within Hamming distance *d* differ in at most *d* bits, so if
*d* < 4 at least one band contains none of the differing bits and matches
exactly — probing for an exact match on any band returns every true
near-duplicate. Recall is 100%, not approximate.

**That guarantee holds only while `NEAR_DUP_BANDS > NEAR_DUP_MAX_HAMMING`.**
Raising the threshold to 5 against 4 bands degrades recall silently: duplicates
simply start flowing, with no error anywhere. `archive/dedupe.py` asserts the
relationship at import rather than trusting a comment.

Measured on 100k seeded items (27k originals in a 72 h window): candidate set
per band probe **p50 3, p95 14, p99 27, max 104** — tens, not thousands, so the
four band indexes are probed with `BitmapOr` and no `UNION ALL` rewrite is
warranted. The probe replaced a linear scan that shipped every in-window
original to the client: **30 ms → 1.15 ms p50**, and the new cost does not grow
with the window, where the old one extrapolated to ~1.5 s per ingested item at
a production-sized 72 h window.

### Volume and identity (0004)

| Table | Purpose |
|---|---|
| `routing_stats` | Per-day counters for routing outcomes that no longer keep a per-item row — see [What `routing_decisions` no longer holds](#what-routing_decisions-no-longer-holds). |
| `legacy_item_index` | `item_id → archive_dt` for the pre-UUIDv7 items, whose archive date is not derivable from the id. **Frozen**: no UUIDv4 item id is ever minted again, so nothing writes here after 0004 and it never grows. |

## Item identity

Since migration 0004 `items.item_id` is a **UUIDv7** (RFC 9562) generated by
the application, not `gen_random_uuid()` on the server. Both server defaults on
`items` were dropped — `item_id` and `first_seen_utc` — because with the writer
always supplying a value, a default silently papers over any insert path that
forgets.

Two properties are load-bearing rather than incidental:

- **Self-locating.** UUIDv7 embeds a 48-bit millisecond timestamp, so
  `archive.ids.item_id_to_dt()` names an item's `dt=` partition arithmetically.
  That is what lets a label created a year from now, carrying nothing but an
  `item_id`, find its item after every trace of it has left PostgreSQL. A
  lookup table would need retention of its own and would eventually be purged;
  see [the feedback contract](adr/feedback-contract.md).
- **Time-ordered.** Index inserts land at the right-hand edge of the B-tree
  instead of scattering across it, which at 500k items/day is the difference
  between appending and rewriting random pages.

`first_seen_utc` is stamped from the id's own timestamp rather than `now()`, so
the column the partition is keyed on and the date embedded in the id cannot
drift apart. A side effect worth knowing: `now()` inside the per-fetch
transaction is *transaction-start* time, so a fetch cycle spanning midnight
used to stamp items with the previous day. Deriving removes that skew.

## Writing a migration that backfills

Three rules, learned from what each one prevents. They apply to any migration
that reads from a live table.

1. **Lift the role timeouts.** `SET LOCAL statement_timeout = 0` and
   `SET LOCAL idle_in_transaction_session_timeout = 0` at the top of the
   transaction. Migration 0003 set both on the role and migrations run as that
   role; every real backfill exceeds 30 s at production size. `SET LOCAL`
   reverts at commit, so normal operation keeps the bound. Before an
   `ACCESS EXCLUSIVE` operation also set `lock_timeout` so the migration fails
   fast instead of queueing ahead of every query behind it.
2. **Hold the fetch lock across the backfill.**
   `pg_try_advisory_xact_lock(hashtext('newsbot:fetch'))` — the *xact* form,
   never the session-scoped `pg_advisory_lock()`, which does not release at
   commit and would leave ingest dead with no sign of why if the migration
   raised. Acquiring is itself a statement, so take it after rule 1. On retry
   exhaustion **fail with the schema untouched**; proceeding without the lock
   reintroduces the lost-write bug it exists to prevent.
3. **Catch up, then verify.** Re-scan for rows written since the snapshot,
   bounded on `first_seen_utc > snapshot - interval '5 minutes'` — the margin
   is deliberate, because `first_seen_utc` is now minted client-side while the
   snapshot comes from the database, and the pass is idempotent so
   over-scanning is free while under-scanning is a silent hole. Then compare
   source and destination counts and abort on a mismatch.

## Retention

**Every table has a row here, including the ones whose answer is "never".** On
a disk that cannot grow, a table nobody assigned a policy to is how you run out
of space. `tests/test_schema_inventory.py` fails if a migration adds a table
and does not add a line below.

Deletion lives in `archive/retention.py` (operational windows) and
`archive/errors.py` (the error tables).

| Table | When deleted | Default | Configured by |
|---|---|---|---|
| `items` | archived to Parquet, then the day's partition is dropped | 7 days | `ARCHIVE_RETENTION_DAYS` |
| `item_categories` | with the item's partition | 7 days | as above |
| `routing_decisions` | with the item's partition | 7 days | as above |
| `deliveries` | with the item's partition | 7 days | as above |
| `item_keys` | own TTL, deliberately longer than the item window; chunked | 30 days | `ITEM_KEYS_TTL_DAYS` |
| `fetches` | older than the window | 90 days | `FETCHES_TTL_DAYS` |
| `seen_updates` | older than the window, every execution | 24 h | `SEEN_UPDATES_TTL_HOURS` |
| `error_occurrences` | older than the window | 30 days | `errors.retention.occurrences_days` |
| `alert_outbox` | delivered, same window as occurrences | 30 days | as above |
| `error_events` | resolved, older than the window, **and** no surviving occurrences | 365 days | `errors.retention.events_days` |
| `routing_stats` | **never** — counters, a few hundred rows a day | — | — |
| `legacy_item_index` | **never** — frozen at migration 0004, cannot grow | — | — |
| `spec_versions` | **never** — every archived row references one | — | — |
| `translations` | **never** — content-keyed cache; re-deleting re-bills the provider | — | — |
| `source_state`, `channel_state` | **never** — one row per spec entry | — | — |
| `bot_state` | **never** — a handful of keys | — | — |
| `operator_actions` | **never** — audit trail | — | — |

Ordering dependency: the `fetches` purge is **inert until migration 0006**.
`items.fetch_id` carries a foreign key to `fetches` until then, so deleting
inside the window would fail on rows still referenced.
`archive/retention.py` checks for the constraint and returns 0 while it exists.

Retention of the error tables runs at most once per 24 h (tracked in
`bot_state`); the operational windows run every execution — including in
long-running mode, which previously skipped them.

### What `routing_decisions` no longer holds

At 500k items/day, one row per item per bound channel makes
`routing_decisions` the largest table in the system, and the negative outcomes
are only ever read in aggregate. Since migration 0004 only the outcomes in
`archive.writer.RETAINED_DECISIONS` — `routed`, `rate_limited`, `duplicate` —
keep a per-item row; the rest increment a per-day counter in `routing_stats`.
`rate_limited` must stay because the postable backlog is a query over it.

Two consequences worth stating plainly:

- **The per-item negative decisions are gone and cannot come back.** "Why was
  *this* item filtered three weeks ago" is answerable only for the retained
  set; for the rest you get counts per day, channel and outcome. Migration
  0004's `downgrade()` restores the schema but not those rows.
- **The counters are not idempotent.** A counter has no per-item identity to
  conflict on, so a crash-retried fetch cycle can double-count a negative
  outcome. Accepted: it is a debugging aggregate, not an accounting record.

## Timezone discipline

Every timestamp column is `timestamptz`; sessions run with `timezone=UTC`;
the row models (`archive/models.py`) use pydantic `AwareDatetime`, so a naive
datetime is rejected before it can reach SQL.
