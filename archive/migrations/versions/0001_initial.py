"""Initial schema: spec versions, source/channel state, fetches, items with
dedupe columns, item categories, translations, routing decisions, deliveries.

Revision ID: 0001
Revises:
Create Date: 2026-08-01
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE source_kind AS ENUM
  ('general_news','central_bank','statistics','press_wire','exchange');

CREATE TYPE routing_decision_t AS ENUM
  ('routed','filtered_category','predicate_failed','too_old',
   'duplicate','channel_disabled','cold_start_skip','rate_limited');

CREATE TYPE delivery_status AS ENUM ('sent','failed','skipped');

-- Every archived row points at the exact spec that produced it. Without this
-- you cannot interpret the archive after the spec changes.
CREATE TABLE spec_versions (
  spec_hash   text PRIMARY KEY,
  first_seen_utc timestamptz NOT NULL DEFAULT now(),
  spec        jsonb NOT NULL
);

CREATE TABLE source_state (
  source_name     text PRIMARY KEY,
  next_fetch_utc  timestamptz NOT NULL DEFAULT now(),
  last_fetch_utc  timestamptz,
  etag            text,
  last_modified   text,
  cold_start_done boolean NOT NULL DEFAULT false
);

CREATE TABLE channel_state (
  channel_name  text PRIMARY KEY,
  last_post_utc timestamptz
);

CREATE TABLE fetches (
  fetch_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_name    text NOT NULL,
  started_utc    timestamptz NOT NULL,
  finished_utc   timestamptz,
  http_status    int,
  etag           text,
  last_modified  text,
  item_count     int NOT NULL DEFAULT 0,
  new_item_count int NOT NULL DEFAULT 0,
  possible_gap   boolean NOT NULL DEFAULT false,
  spec_hash      text NOT NULL REFERENCES spec_versions(spec_hash),
  error          text
);
CREATE INDEX ON fetches (source_name, started_utc DESC);

CREATE TABLE items (
  item_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_name     text NOT NULL,
  fetch_id        uuid NOT NULL REFERENCES fetches(fetch_id),
  source_item_id  text NOT NULL,
  raw_xml         bytea NOT NULL,
  title           text NOT NULL,
  lead_original   text,
  lead_language   text,
  url_raw         text NOT NULL,
  canonical_url   text NOT NULL,
  image_url       text,
  published_raw   text,
  published_utc   timestamptz,
  first_seen_utc  timestamptz NOT NULL DEFAULT now(),
  content_hash    bytea NOT NULL,
  title_simhash   bigint NOT NULL,
  duplicate_of    uuid REFERENCES items(item_id),
  copyright_holder text,
  spec_hash       text NOT NULL REFERENCES spec_versions(spec_hash),
  UNIQUE (source_name, source_item_id)
);
CREATE INDEX ON items (published_utc DESC);
CREATE INDEX ON items (first_seen_utc DESC);
CREATE INDEX ON items (canonical_url);
CREATE INDEX ON items (content_hash);
CREATE INDEX ON items (source_name, first_seen_utc DESC);

CREATE TABLE item_categories (
  item_id  uuid NOT NULL REFERENCES items(item_id),
  ordinal  int  NOT NULL,
  category text NOT NULL,
  PRIMARY KEY (item_id, ordinal)
);
CREATE INDEX ON item_categories (category);

-- Translation cache, keyed by content so re-runs never re-bill a provider.
CREATE TABLE translations (
  content_hash    bytea NOT NULL,
  field           text  NOT NULL,
  target_language text  NOT NULL,
  translated      text  NOT NULL,
  provider        text  NOT NULL,
  created_utc     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (content_hash, field, target_language)
);

CREATE TABLE routing_decisions (
  item_id        uuid NOT NULL REFERENCES items(item_id),
  channel_name   text NOT NULL,
  decision       routing_decision_t NOT NULL,
  reason         text,
  matched_clause jsonb,
  decided_utc    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (item_id, channel_name)
);
CREATE INDEX ON routing_decisions (channel_name, decision, decided_utc DESC);

CREATE TABLE deliveries (
  item_id             uuid NOT NULL REFERENCES items(item_id),
  channel_name        text NOT NULL,
  chat_id             text NOT NULL,
  telegram_message_id bigint,
  posted_utc          timestamptz,
  status              delivery_status NOT NULL,
  attempt             int NOT NULL DEFAULT 1,
  error               text,
  PRIMARY KEY (item_id, channel_name)
);
CREATE INDEX ON deliveries (channel_name, posted_utc DESC);
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS deliveries, routing_decisions, translations,
          item_categories, items, fetches, channel_state, source_state,
          spec_versions CASCADE;
        DROP TYPE IF EXISTS delivery_status, routing_decision_t, source_kind;
        """
    )
