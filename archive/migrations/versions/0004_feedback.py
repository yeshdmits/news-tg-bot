"""Admin feedback loop: approve/reject labels on archived items.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
CREATE TYPE review_state AS ENUM ('queued', 'pending', 'decided');

-- One review per item. A source has exactly one review chat, so item_id is
-- the natural key; the row is created when the item is first archived and
-- then only moves forward through the state enum.
--
-- This is dataset, not operational state: nothing here is ever deleted or
-- purged, matching the keep-everything stance of the content tables.
CREATE TABLE feedback_reviews (
  item_id             uuid PRIMARY KEY REFERENCES items(item_id),
  source_name         text NOT NULL,
  chat_id             text NOT NULL,
  state               review_state NOT NULL DEFAULT 'queued',
  queued_utc          timestamptz NOT NULL DEFAULT now(),
  -- the card currently in the chat, NULL until it has been sent
  message_id          bigint,
  sent_utc            timestamptz,
  -- the label itself; NULL for everything not yet decided
  approved            boolean,
  decided_utc         timestamptz,
  decided_by_user_id  bigint,
  decided_by_username text,
  spec_hash           text NOT NULL REFERENCES spec_versions(spec_hash)
);

-- The FIFO scan: oldest queued row for a chat.
CREATE INDEX ON feedback_reviews (chat_id, state, queued_utc);

-- One card per chat at a time, enforced by the database rather than by
-- application ordering. The fetch job and the webhook app both send cards,
-- so two processes can reach for the same chat concurrently; this index
-- makes the loser fail instead of double-posting.
CREATE UNIQUE INDEX feedback_reviews_one_pending
  ON feedback_reviews (chat_id) WHERE state = 'pending';

-- Export/statistics scans read every decided row.
CREATE INDEX ON feedback_reviews (state, decided_utc);
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS feedback_reviews;
        DROP TYPE IF EXISTS review_state;
        """
    )
