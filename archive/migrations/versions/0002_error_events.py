"""Error handling and operator alerting: grouped error events with raw
occurrences, alert outbox, operator audit trail, key/value bot state.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-01
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
CREATE TYPE error_severity AS ENUM ('critical','error','warning','info');
CREATE TYPE error_state    AS ENUM ('unacked','acked','resolved');

CREATE TABLE error_events (
  event_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fingerprint         text NOT NULL UNIQUE,
  short_id            text NOT NULL UNIQUE,
  severity            error_severity NOT NULL,
  state               error_state NOT NULL DEFAULT 'unacked',
  component           text NOT NULL,        -- fetcher|feedspec|archive|newsbot|telegram|translator
  error_class         text NOT NULL,
  source_name         text,
  channel_name        text,
  item_id             uuid REFERENCES items(item_id),
  message             text NOT NULL,
  detail              jsonb,                -- stack trace, HTTP body, offending XML fragment
  first_seen_utc      timestamptz NOT NULL DEFAULT now(),
  last_seen_utc       timestamptz NOT NULL DEFAULT now(),
  occurrence_count    int NOT NULL DEFAULT 1,
  consecutive_streak  int NOT NULL DEFAULT 1,
  alert_count         int NOT NULL DEFAULT 0,
  last_alert_utc      timestamptz,
  -- the single message this fingerprint owns in the ops chat
  alert_chat_id       text,
  alert_message_id    bigint,
  alert_thread_id     bigint,
  pinned              boolean NOT NULL DEFAULT false,
  muted_until_utc     timestamptz,
  muted_by_username   text,
  acked_by_user_id    bigint,
  acked_by_username   text,
  acked_utc           timestamptz,
  resolved_utc        timestamptz,
  spec_hash           text REFERENCES spec_versions(spec_hash)
);
CREATE INDEX ON error_events (state, severity, last_seen_utc DESC);
CREATE INDEX ON error_events (source_name, last_seen_utc DESC);
CREATE INDEX ON error_events (alert_chat_id, alert_message_id);

-- Individual occurrences, for forensics. Capped by retention.
CREATE TABLE error_occurrences (
  occurrence_id bigserial PRIMARY KEY,
  fingerprint   text NOT NULL REFERENCES error_events(fingerprint),
  occurred_utc  timestamptz NOT NULL DEFAULT now(),
  detail        jsonb
);
CREATE INDEX ON error_occurrences (fingerprint, occurred_utc DESC);

-- Alerts that could not be delivered. Flushed on next successful contact.
CREATE TABLE alert_outbox (
  outbox_id     bigserial PRIMARY KEY,
  fingerprint   text NOT NULL,
  body          text NOT NULL,
  queued_utc    timestamptz NOT NULL DEFAULT now(),
  delivered_utc timestamptz,
  attempts      int NOT NULL DEFAULT 0,
  last_error    text
);

-- Audit of who did what, since several people share the chat.
CREATE TABLE operator_actions (
  action_id    bigserial PRIMARY KEY,
  acted_utc    timestamptz NOT NULL DEFAULT now(),
  user_id      bigint NOT NULL,
  username     text,
  command      text NOT NULL,
  target       text,
  result       text
);

CREATE TABLE bot_state (
  key   text PRIMARY KEY,
  value text NOT NULL
);  -- getUpdates offset, alert budget window, resolved ops chat id, admin cache

-- fetch-failure streak tracking
ALTER TABLE source_state ADD COLUMN consecutive_failures int NOT NULL DEFAULT 0;
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE source_state DROP COLUMN consecutive_failures;
        DROP TABLE IF EXISTS bot_state, operator_actions, alert_outbox,
          error_occurrences, error_events CASCADE;
        DROP TYPE IF EXISTS error_state, error_severity;
        """
    )
