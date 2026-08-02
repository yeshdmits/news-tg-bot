"""Serverless execution: webhook idempotency and role timeouts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-01
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
-- Webhook idempotency: one row per Telegram update_id. A monotonic offset
-- (the old getUpdates model) is wrong for webhooks — a retry can redeliver
-- an older update_id after a newer one succeeded. Rows expire after 24h.
CREATE TABLE seen_updates (
  update_id bigint PRIMARY KEY,
  seen_utc  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON seen_updates (seen_utc);

-- Serverless invocations open and close sessions constantly; a killed
-- container must not strand a lock or an idle transaction.
ALTER ROLE CURRENT_USER SET statement_timeout = '30s';
ALTER ROLE CURRENT_USER SET idle_in_transaction_session_timeout = '60s';
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS seen_updates;
        ALTER ROLE CURRENT_USER RESET statement_timeout;
        ALTER ROLE CURRENT_USER RESET idle_in_transaction_session_timeout;
        """
    )
