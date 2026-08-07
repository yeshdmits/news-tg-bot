# The feedback contract

## Status

Reserved. **Nothing here is implemented** — no table, no enum, no migration.

This document exists so the feature can be built later without a rewrite. The
scaling work deliberately did the parts that cannot be retrofitted (UUIDv7 ids,
`item_id`-sorted Parquet, working sidecar joins) and stopped at the parts that
can.

## Context

A future feature adds a separate Telegram chat where an approved user is shown
news items and answers 👍/👎. Those labels become the training set for a
personal relevance model, matched against the whole archived corpus.

The hard requirement, and the reason this document exists:

> A label created at an arbitrary future date, carrying only an `item_id`, must
> be joinable to that item's archived record — even if the item was archived
> months earlier and every trace of it has left PostgreSQL.

That rules out any design resting on a lookup table with a TTL. It also had to
be decided *before* the archive was written, because changing the id scheme
after millions of rows exist is not a migration worth attempting.

## What already exists for it

- **`item_id` is UUIDv7** (migration 0004). `archive.ids.item_id_to_dt()`
  derives an item's `dt=` partition from its id arithmetically — no lookup, no
  TTL, no storage cost.
- **`items.first_seen_utc` is stamped from the id's own timestamp**, so the
  partition key and the derived date cannot disagree.
- **`legacy_item_index`** covers the pre-UUIDv7 items. Frozen; never grows.
- **Each day's Parquet is sorted by `item_id`**, so a point lookup reads one
  row group rather than the whole file.
- **`feedback/dt=…` is reserved** in the archive layout alongside
  `embeddings/` and `clusters/`.

## What the implementation must satisfy

**The join key is `item_id`, alone.** Not `(source_name, source_item_id)`, not
a URL, not a Telegram message id. Those are all either mutable or absent from
the archived record.

**Locate the item with `archive.ids.archive_dt_candidates(item_id)`.** For a
UUIDv7 it returns exactly one date; do not widen it to a ±1 window, which would
triple the I/O of every lookup to absorb a skew that cannot occur. An **empty**
list means the id is UUIDv4 — fall back to `legacy_item_index`, then
`legacy_dt_candidates()`, which *does* span ±1 day because those rows took
`first_seen_utc` from `now()`.

**Labels are permanent and exempt from every retention policy.** They are the
scarcest data in the system — each one costs a human interaction — and unlike
items they cannot be re-fetched. A label must outlive the item it points at;
that is the normal case, not the edge case. It follows that `item_feedback`
never gets a partition, never gets a TTL, and never appears in
`archive/retention.py`. It still needs a row in `docs/data-model.md`'s
retention table saying **never**.

**A label may reference an `item_id` that is no longer in `items`.** So
`item_feedback` must **not** carry a foreign key to `items` — it would block
partition drops, which is exactly the hazard `error_events.item_id` already
demonstrates.

**The weekly archive job exports labels to `feedback/dt=…`**, partitioned by
the *item's* `dt`, not the label's creation date, so a sidecar join needs no
extra index. `archive/archiver.py` already has the call site as a documented
no-op; fill it in rather than adding a new step.

**Reuse `seen_updates` for `callback_query` idempotency.** Telegram redelivers
callback queries on any non-200, and out of order. ADR 0003 explains why a
monotonic offset is wrong for webhooks; the same reasoning applies here, and
the mechanism already exists.

## Suggested shape

Not binding — the constraints above are. Sketched so the reserved space is
concrete:

```sql
CREATE TABLE item_feedback (
  item_id     uuid        NOT NULL,   -- deliberately NO foreign key
  chat_id     text        NOT NULL,
  user_id     bigint      NOT NULL,
  label       smallint    NOT NULL,   -- +1 / -1
  labelled_utc timestamptz NOT NULL DEFAULT now(),
  archive_dt  date,                   -- backfilled by the archive job
  PRIMARY KEY (item_id, user_id)
);
```

`archive_dt` is a convenience for export, not the lookup mechanism — the
lookup is always derived from `item_id`. It is nullable precisely so a label
written today for an item archived last year is legal.

## Verification that already runs

`tests/test_dataset_join.py` (Phase 6) proves the whole path with nothing but
an `item_id`: archive ten backdated days, confirm the items are gone from
PostgreSQL, write a synthetic label into `feedback/dt=…` exactly as this
feature would, and assert `load_with_sidecar` returns the item joined to its
label with full text and metadata. If that test passes, this contract is
satisfiable.

## Out of scope here

The chat itself, approved-user gating, inline keyboards, `callback_query`
handling, the table, and the model. Only the contract and the archive
properties it depends on.
