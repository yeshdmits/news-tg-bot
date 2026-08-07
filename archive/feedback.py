"""The only module that writes ``feedback_reviews``.

The queue is FIFO per review chat and shows exactly one card at a time.
Two processes send cards — the fetch job (seed and self-heal) and the
webhook app (the steady-state loop) — so every state transition here is a
single conditional statement whose ``RETURNING`` clause says whether this
caller won. Nothing depends on application-level ordering:

    queued  --claim_next-->  pending  --decide-->  decided
       ^                        |
       +------release_claim-----+   (the send failed)

``decide`` is the only writer of ``approved``, and it writes it before any
Telegram traffic happens — a label is never lost because a message could
not be deleted.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from archive.models import ReviewRecord

_COLUMNS = """
    item_id, source_name, chat_id, state, queued_utc, message_id, sent_utc,
    approved, decided_utc, decided_by_user_id, decided_by_username, spec_hash
"""


def _record(row: dict[str, Any] | None) -> ReviewRecord | None:
    return ReviewRecord(**row) if row else None


def enqueue(
    conn: psycopg.Connection,
    item_id: UUID,
    *,
    source_name: str,
    chat_id: str,
    spec_hash: str,
) -> bool:
    """Queue a freshly archived item for review; returns False when it was
    already queued. Idempotent so a crash-retried fetch cycle is a no-op."""
    row = conn.execute(
        """
        INSERT INTO feedback_reviews (item_id, source_name, chat_id, spec_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (item_id) DO NOTHING
        RETURNING item_id
        """,
        (item_id, source_name, chat_id, spec_hash),
    ).fetchone()
    return row is not None


def claim_next(
    conn: psycopg.Connection, chat_id: str, source_names: list[str]
) -> ReviewRecord | None:
    """Claim the oldest queued item for a chat, or None when the queue is
    empty or a card is already showing.

    The claim is taken *before* the send so two processes cannot both build
    a card for the same chat. The NOT EXISTS guard keeps the common case off
    the error path; the partial unique index is what actually enforces the
    invariant when two claims interleave inside the same instant.

    ``source_names`` is the set of sources still present in the spec. Items
    of a source that has since been removed are skipped, not dropped: they
    stay queued and resume if the source comes back, and meanwhile they
    cannot stall a chat that other sources also feed."""
    row = conn.execute(
        f"""
        UPDATE feedback_reviews SET state = 'pending', sent_utc = now()
        WHERE item_id = (
            SELECT item_id FROM feedback_reviews
            WHERE chat_id = %(chat)s AND state = 'queued'
              AND source_name = ANY(%(sources)s)
            ORDER BY queued_utc, item_id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
          )
          AND NOT EXISTS (
            SELECT 1 FROM feedback_reviews
            WHERE chat_id = %(chat)s AND state = 'pending'
          )
        RETURNING {_COLUMNS}
        """,
        {"chat": chat_id, "sources": list(source_names)},
    ).fetchone()
    return _record(row)


def attach_message(conn: psycopg.Connection, item_id: UUID, message_id: int) -> None:
    """Record the card's Telegram message id after a successful send."""
    conn.execute(
        "UPDATE feedback_reviews SET message_id = %s WHERE item_id = %s",
        (message_id, item_id),
    )


def release_claim(conn: psycopg.Connection, item_id: UUID) -> None:
    """Undo a claim whose send failed, so the item returns to the head of
    the queue instead of blocking its chat forever."""
    conn.execute(
        """
        UPDATE feedback_reviews SET state = 'queued', sent_utc = NULL
        WHERE item_id = %s AND state = 'pending'
        """,
        (item_id,),
    )


def orphaned_claim(conn: psycopg.Connection, chat_id: str) -> ReviewRecord | None:
    """A claim with no card: the process died between claiming and sending
    (or between sending and recording the message id — the send is retried,
    which at worst leaves one stale card in the chat). Blocks the chat until
    resent, so the fetch job looks for these first."""
    row = conn.execute(
        f"""
        SELECT {_COLUMNS} FROM feedback_reviews
        WHERE chat_id = %s AND state = 'pending' AND message_id IS NULL
        """,
        (chat_id,),
    ).fetchone()
    return _record(row)


def pending_card(conn: psycopg.Connection, chat_id: str) -> ReviewRecord | None:
    """The card currently showing in a chat, if any."""
    row = conn.execute(
        f"""
        SELECT {_COLUMNS} FROM feedback_reviews
        WHERE chat_id = %s AND state = 'pending'
        """,
        (chat_id,),
    ).fetchone()
    return _record(row)


def decide(
    conn: psycopg.Connection,
    item_id: UUID,
    *,
    approved: bool,
    user_id: int | None,
    username: str | None,
) -> ReviewRecord | None:
    """Record the label. Returns the decided row, or None when the item was
    not pending — a second admin pressing the same card, or a button on a
    card that has already been answered. Exactly one caller ever wins."""
    row = conn.execute(
        f"""
        UPDATE feedback_reviews
        SET state = 'decided', approved = %s, decided_utc = now(),
            decided_by_user_id = %s, decided_by_username = %s
        WHERE item_id = %s AND state = 'pending'
        RETURNING {_COLUMNS}
        """,
        (approved, user_id, username, item_id),
    ).fetchone()
    return _record(row)


def get_review(conn: psycopg.Connection, item_id: UUID) -> ReviewRecord | None:
    """One review row by item id, whatever its state."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM feedback_reviews WHERE item_id = %s", (item_id,)
    ).fetchone()
    return _record(row)


def queue_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-chat queue depth and label tally, for `cli stats`."""
    return conn.execute(
        """
        SELECT chat_id,
               count(*) FILTER (WHERE state = 'queued')  AS queued,
               count(*) FILTER (WHERE state = 'pending') AS pending,
               count(*) FILTER (WHERE approved)          AS approved,
               count(*) FILTER (WHERE approved IS FALSE) AS rejected
        FROM feedback_reviews
        GROUP BY chat_id
        ORDER BY chat_id
        """
    ).fetchall()
