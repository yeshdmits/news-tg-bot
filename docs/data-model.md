# Data model

One PostgreSQL database, schema owned by alembic
(`archive/migrations/versions/`, four migrations, plain SQL). The `archive`
package is the **only** code that writes to it.

## Design stance: keep everything

The archive is append-only for content. Every fetched item is stored
permanently, **including the raw XML fragment it was parsed from**, along
with every fetch, every routing decision, and every delivery attempt. Nothing
in this list is ever deleted or pruned — unbounded growth is deliberate, not
an oversight:

- the raw XML makes every item re-parseable later (a test asserts that
  re-parsing a stored blob with the same mapping reproduces the item);
- the decision/delivery trail answers "why did/didn't this post?" months
  later;
- `cli export` turns the archive into parquet/csv for analysis.

Only operational error data is ever purged (see [Retention](#retention)).
If disk growth matters to you, `cli stats` reports per-source volumes; plan
Postgres storage accordingly.

## Tables

### Content and provenance (migration 0001)

| Table | One row per | Notes |
|---|---|---|
| `spec_versions` | distinct spec.json content | `spec_hash` (sha256) + the full spec as jsonb. Every fetch, item and error event references the exact spec version that produced it, so the archive stays interpretable after config changes. |
| `fetches` | HTTP fetch of a source | Status, ETag/Last-Modified, item counts, `possible_gap` (feed moved faster than the fetch cadence), error text for failed fetches. |
| `items` | item first seen | `raw_xml` (bytea, NOT NULL), extracted fields, `canonical_url` (tracking params stripped), `content_hash`, `title_simhash`, `duplicate_of` self-reference, `copyright_holder`. `UNIQUE (source_name, source_item_id)` is the idempotency anchor: re-fetching the same feed inserts nothing new. |
| `item_categories` | (item, ordinal) | Categories in feed order. |
| `translations` | (content_hash, field, target_language) | Content-keyed translation cache — re-runs and duplicates never re-bill the provider. |
| `routing_decisions` | (item, channel) | Exactly one decision per item per bound channel: `routed`, `filtered_category`, `predicate_failed`, `too_old`, `duplicate`, `channel_disabled`, `cold_start_skip`, or `rate_limited` (with a `reason`; `gated:` reasons are re-evaluated next window, others are terminal). An [archive-only](configuration.md#archive-only-sources) source has no bound channels and so produces **no** rows here — its items are stored with no decision trail at all. `channel_disabled` doubles as the retirement state for a decision whose binding has since disappeared from the spec, carrying the reason `binding removed from spec`. |
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

### Admin feedback (0004)

| Table | One row per | Notes |
|---|---|---|
| `feedback_reviews` | reviewed item | The human label. `approved` (boolean, NULL until answered) plus who answered and when, and the card's Telegram `message_id` while it is showing. `state` walks `queued → pending → decided`; a partial unique index on `(chat_id) WHERE state = 'pending'` is what enforces **one card per review chat at a time**, since both the fetch job and the webhook app send cards. Written when the item is first archived, for sources with a [`feedback`](configuration.md#feedback-sourcesfeedback) chat, duplicates excluded — and topped up from `items` whenever a chat's queue runs dry, so an archive that predates the feature still gets labelled. |

This is dataset, not operational state, so it follows the keep-everything
stance above: no retention, nothing purged. `cli export` joins it 1:1 onto
`items` as `review_state` / `review_approved` / `review_decided_utc` /
`review_decided_by_user_id`, which is what turns the archive from a corpus
into a training set. The label is written before any Telegram traffic
happens, so a card that cannot be deleted never costs a decision.

Migration 0003 also sets role-level `statement_timeout = '30s'` and
`idle_in_transaction_session_timeout = '60s'` — a killed serverless container
must not strand a lock or an idle transaction.

## Retention

All deletion in the codebase happens in two functions
(`archive/errors.py`):

| What | When deleted | Default |
|---|---|---|
| `seen_updates` rows | older than 24 h, purged every execution | fixed |
| `error_occurrences` | older than `errors.retention.occurrences_days` | 30 days |
| `alert_outbox` (delivered) | same window as occurrences | 30 days |
| `error_events` | resolved, older than `errors.retention.events_days`, **and** no surviving occurrences | 365 days |

Retention runs at most once per 24 h (tracked in `bot_state`). Everything
else — items, raw XML, fetches, deliveries, routing decisions, translations,
feedback reviews — is kept forever.

## Timezone discipline

Every timestamp column is `timestamptz`; sessions run with `timezone=UTC`;
the row models (`archive/models.py`) use pydantic `AwareDatetime`, so a naive
datetime is rejected before it can reach SQL.
