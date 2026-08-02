"""Error-event persistence: fingerprinting, grouping, ack/mute, outbox,
operator audit, bot state.

All SQL over the error/ops tables lives here — archive stays the only writer.
Policy (when to alert, how loudly) lives in newsbot.alerts; this module is
mechanical.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

# --- fingerprinting -------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_QUERY_RE = re.compile(r"\?\S*")
_DIGITS_RE = re.compile(r"\d+")


def normalise_message(message: str) -> str:
    """Strip volatile parts so recurring problems group: URL query strings,
    UUIDs and hex blobs → ``*``, digit runs → ``#``. Without this,
    "connection refused to 10.0.0.4:53211" and ":53219" never group."""
    out = _QUERY_RE.sub("", message)
    out = _UUID_RE.sub("*", out)
    out = _HEX_RE.sub("*", out)
    out = _DIGITS_RE.sub("#", out)
    return re.sub(r"\s+", " ", out).strip()


def fingerprint_of(
    component: str,
    error_class: str,
    source_name: str | None,
    channel_name: str | None,
    message: str,
) -> str:
    """Stable grouping key: sha256 over component, error class, source/channel
    and the normalised message."""
    payload = "|".join(
        (component, error_class, source_name or "", channel_name or "", normalise_message(message))
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# --- event upsert -----------------------------------------------------------

FETCH_ERROR_CLASSES = (
    "FetchHttpError4xx", "FetchHttpError5xx", "FetchTimeout",
    "FetchNetworkError", "FeedParseError", "SchemaValidationFailed",
)


def record_occurrence(
    conn: psycopg.Connection,
    *,
    component: str,
    error_class: str,
    severity: str,
    message: str,
    source_name: str | None = None,
    channel_name: str | None = None,
    item_id: UUID | None = None,
    detail: dict[str, Any] | None = None,
    spec_hash: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Upsert the grouped event row and append a forensic occurrence.

    Returns the event row after the update, with ``is_new`` added. Severity is
    only ever raised here, never lowered — escalation decisions belong to the
    caller (set_severity), de-escalation happens via resolve + new event.
    A previously resolved fingerprint that re-occurs reopens as unacked with
    its streak reset to 1.
    """
    fingerprint = fingerprint_of(component, error_class, source_name, channel_name, message)
    row = None
    for short_len in (8, 12, 16, 24):
        try:
            # savepoint-scoped so a short_id collision doesn't poison an
            # enclosing transaction; lengthen the prefix and retry
            with conn.transaction():
                row = conn.execute(
                """
                INSERT INTO error_events
                  (fingerprint, short_id, severity, component, error_class,
                   source_name, channel_name, item_id, message, detail, spec_hash,
                   first_seen_utc, last_seen_utc)
                VALUES (%(fp)s, %(sid)s, %(sev)s, %(comp)s, %(cls)s,
                        %(src)s, %(ch)s, %(item)s, %(msg)s, %(detail)s, %(spec)s,
                        coalesce(%(now)s, now()), coalesce(%(now)s, now()))
                ON CONFLICT (fingerprint) DO UPDATE SET
                  occurrence_count = error_events.occurrence_count + 1,
                  consecutive_streak = CASE
                    WHEN error_events.state = 'resolved' THEN 1
                    ELSE error_events.consecutive_streak + 1 END,
                  state = CASE
                    WHEN error_events.state = 'resolved' THEN 'unacked'::error_state
                    ELSE error_events.state END,
                  resolved_utc = CASE
                    WHEN error_events.state = 'resolved' THEN NULL
                    ELSE error_events.resolved_utc END,
                  last_seen_utc = coalesce(%(now)s, now()),
                  message = %(msg)s,
                  detail = coalesce(%(detail)s, error_events.detail),
                  item_id = coalesce(%(item)s, error_events.item_id),
                  spec_hash = coalesce(%(spec)s, error_events.spec_hash)
                RETURNING *, (xmax = 0) AS is_new
                """,
                    {
                        "fp": fingerprint, "sid": fingerprint[:short_len], "sev": severity,
                        "comp": component, "cls": error_class, "src": source_name,
                        "ch": channel_name, "item": item_id, "msg": message,
                        "detail": Jsonb(detail) if detail is not None else None,
                        "spec": spec_hash, "now": now,
                    },
                ).fetchone()
            break
        except psycopg.errors.UniqueViolation:
            continue
    if row is None:  # pragma: no cover - 24 hex chars colliding is unreachable
        raise RuntimeError("could not allocate a unique short_id")

    conn.execute(
        "INSERT INTO error_occurrences (fingerprint, occurred_utc, detail) "
        "VALUES (%s, coalesce(%s, now()), %s)",
        (fingerprint, now, Jsonb(detail) if detail is not None else None),
    )
    return dict(row)


def set_severity(conn: psycopg.Connection, fingerprint: str, severity: str) -> None:
    """Overwrite an event's severity; escalation policy is the caller's."""
    conn.execute(
        "UPDATE error_events SET severity = %s WHERE fingerprint = %s",
        (severity, fingerprint),
    )


def record_alert(
    conn: psycopg.Connection,
    fingerprint: str,
    *,
    chat_id: str | None = None,
    message_id: int | None = None,
    thread_id: int | None = None,
    pinned: bool | None = None,
    now: datetime | None = None,
) -> None:
    """Bump alert bookkeeping and (optionally) the owned-message coordinates."""
    conn.execute(
        """
        UPDATE error_events SET
          alert_count = alert_count + 1,
          last_alert_utc = coalesce(%s, now()),
          alert_chat_id = coalesce(%s, alert_chat_id),
          alert_message_id = coalesce(%s, alert_message_id),
          alert_thread_id = coalesce(%s, alert_thread_id),
          pinned = coalesce(%s, pinned)
        WHERE fingerprint = %s
        """,
        (now, chat_id, message_id, thread_id, pinned, fingerprint),
    )


def set_alert_message(
    conn: psycopg.Connection, fingerprint: str, chat_id: str, message_id: int
) -> None:
    """Point the event at the Telegram message that now represents its alert."""
    conn.execute(
        "UPDATE error_events SET alert_chat_id = %s, alert_message_id = %s "
        "WHERE fingerprint = %s",
        (chat_id, message_id, fingerprint),
    )


# --- lookups ----------------------------------------------------------------


def get_event(conn: psycopg.Connection, short_id: str) -> dict[str, Any] | None:
    """Fetch one event by its operator-facing short_id, or None."""
    row = conn.execute(
        "SELECT * FROM error_events WHERE short_id = %s", (short_id,)
    ).fetchone()
    return dict(row) if row else None


def get_event_by_alert_message(
    conn: psycopg.Connection, chat_id: str, message_id: int
) -> dict[str, Any] | None:
    """Fetch the event that owns a given Telegram alert message, or None."""
    row = conn.execute(
        "SELECT * FROM error_events WHERE alert_chat_id = %s AND alert_message_id = %s",
        (chat_id, message_id),
    ).fetchone()
    return dict(row) if row else None


def query_events(
    conn: psycopg.Connection,
    *,
    min_severity: str | None = None,
    state: str | None = None,
    unreviewed: bool = False,
    exclude_resolved: bool = False,
    source_name: str | None = None,
    since: datetime | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Filtered event listing for operator commands. Filters AND together;
    ``min_severity`` includes everything at least that severe, ``unreviewed``
    selects never-acked events regardless of state. Ordered most severe first,
    then most recently seen."""
    clauses, params = ["true"], []
    severity_order = ["info", "warning", "error", "critical"]
    if min_severity is not None:
        allowed = severity_order[severity_order.index(min_severity):]
        clauses.append("severity = ANY(%s::error_severity[])")
        params.append(allowed)
    if state is not None:
        clauses.append("state = %s")
        params.append(state)
    if unreviewed:
        # Broke, fixed itself, nobody looked — resolved-but-unreviewed
        # must surface here, so filter on review, not on state.
        clauses.append("acked_utc IS NULL")
    if exclude_resolved:
        clauses.append("state != 'resolved'")
    if source_name is not None:
        clauses.append("source_name = %s")
        params.append(source_name)
    if since is not None:
        clauses.append("last_seen_utc >= %s")
        params.append(since)
    params.extend([limit, offset])
    rows = conn.execute(
        f"""
        SELECT * FROM error_events WHERE {' AND '.join(clauses)}
        ORDER BY severity ASC, last_seen_utc DESC
        LIMIT %s OFFSET %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def recent_occurrences(
    conn: psycopg.Connection, fingerprint: str, limit: int = 5
) -> list[dict[str, Any]]:
    """Latest raw occurrences for a fingerprint, newest first."""
    rows = conn.execute(
        "SELECT * FROM error_occurrences WHERE fingerprint = %s "
        "ORDER BY occurred_utc DESC LIMIT %s",
        (fingerprint, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def unacked_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Unacked event counts keyed by severity name."""
    rows = conn.execute(
        "SELECT severity::text AS severity, count(*) AS n FROM error_events "
        "WHERE state = 'unacked' GROUP BY severity"
    ).fetchall()
    return {r["severity"]: r["n"] for r in rows}


# --- operator actions -------------------------------------------------------


def ack_event(
    conn: psycopg.Connection, short_id: str, user_id: int, username: str | None
) -> tuple[str, dict[str, Any] | None]:
    """Race-guarded ack: first acker wins, later attempts see the original.
    Returns ("acked" | "already" | "missing", row)."""
    row = conn.execute(
        """
        UPDATE error_events SET state = 'acked', acked_by_user_id = %s,
          acked_by_username = %s, acked_utc = now()
        WHERE short_id = %s AND acked_by_user_id IS NULL
        RETURNING *
        """,
        (user_id, username, short_id),
    ).fetchone()
    if row:
        return "acked", dict(row)
    existing = get_event(conn, short_id)
    if existing is None:
        return "missing", None
    return "already", existing


def ack_all(
    conn: psycopg.Connection, user_id: int, username: str | None
) -> int:
    """Ack every currently unacked event; returns how many were acked."""
    rows = conn.execute(
        """
        UPDATE error_events SET state = 'acked', acked_by_user_id = %s,
          acked_by_username = %s, acked_utc = now()
        WHERE state = 'unacked' AND acked_by_user_id IS NULL
        RETURNING short_id
        """,
        (user_id, username),
    ).fetchall()
    return len(rows)


def mute(
    conn: psycopg.Connection,
    *,
    short_id: str | None = None,
    source_name: str | None = None,
    until: datetime | None,
    username: str | None,
) -> int:
    """Mute one event (by short_id) or every event of a source until the given
    time; ``until=None`` unmutes. Returns rows affected; raises ValueError when
    neither target is given."""
    if short_id is not None:
        target_sql, target = "short_id = %s", short_id
    elif source_name is not None:
        target_sql, target = "source_name = %s", source_name
    else:
        raise ValueError("mute needs a short_id or a source_name")
    rows = conn.execute(
        f"UPDATE error_events SET muted_until_utc = %s, muted_by_username = %s "
        f"WHERE {target_sql} RETURNING short_id",
        (until, username, target),
    ).fetchall()
    return len(rows)


def record_operator_action(
    conn: psycopg.Connection,
    *,
    user_id: int,
    username: str | None,
    command: str,
    target: str | None,
    result: str,
) -> None:
    """Append one row to the operator audit trail."""
    conn.execute(
        "INSERT INTO operator_actions (user_id, username, command, target, result) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, username, command, target, result),
    )


# --- resolution -----------------------------------------------------


def resolve_fetch_events(
    conn: psycopg.Connection, source_name: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """A source recovered: resolve its open fetch-related events. Returns the
    resolved rows (the caller edits any owned alert messages in place)."""
    rows = conn.execute(
        """
        UPDATE error_events SET state = 'resolved',
          resolved_utc = coalesce(%s, now()), consecutive_streak = 0
        WHERE source_name = %s AND state != 'resolved'
          AND error_class = ANY(%s)
        RETURNING *
        """,
        (now, source_name, list(FETCH_ERROR_CLASSES)),
    ).fetchall()
    return [dict(r) for r in rows]


# --- outbox --------------------------------------------------------


def queue_outbox(conn: psycopg.Connection, fingerprint: str, body: str, error: str) -> None:
    """Queue an alert body whose send failed, for later redelivery."""
    conn.execute(
        "INSERT INTO alert_outbox (fingerprint, body, attempts, last_error) "
        "VALUES (%s, %s, 1, %s)",
        (fingerprint, body, error),
    )


def pending_outbox(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Undelivered outbox rows, oldest first."""
    rows = conn.execute(
        "SELECT * FROM alert_outbox WHERE delivered_utc IS NULL ORDER BY queued_utc"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_outbox_delivered(conn: psycopg.Connection, outbox_ids: list[int]) -> None:
    """Stamp the given outbox rows delivered; an empty list is a no-op."""
    if outbox_ids:
        conn.execute(
            "UPDATE alert_outbox SET delivered_utc = now() WHERE outbox_id = ANY(%s)",
            (outbox_ids,),
        )


# --- bot state --------------------------------------------------------------


def get_state(conn: psycopg.Connection, key: str) -> str | None:
    """Read a bot_state value, or None when the key is unset."""
    row = conn.execute("SELECT value FROM bot_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: psycopg.Connection, key: str, value: str) -> None:
    """Upsert one bot_state key/value pair."""
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


# --- webhook idempotency ----------------------------------------------------


def mark_update_seen(conn: psycopg.Connection, update_id: int) -> bool:
    """Record a webhook update_id; False means it was already processed.
    Telegram redelivers on any non-200, so this is the exactly-once gate."""
    row = conn.execute(
        "INSERT INTO seen_updates (update_id) VALUES (%s) "
        "ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
        (update_id,),
    ).fetchone()
    return row is not None


def purge_seen_updates(conn: psycopg.Connection, *, hours: int = 24) -> int:
    """Delete webhook update_ids older than the window; returns rows removed."""
    rows = conn.execute(
        "DELETE FROM seen_updates WHERE seen_utc < now() - %s RETURNING update_id",
        (timedelta(hours=hours),),
    ).fetchall()
    return len(rows)


# --- retention -----------------------------------------------------


def retention_cleanup(
    conn: psycopg.Connection, *, occurrences_days: int, events_days: int
) -> tuple[int, int]:
    """Age out old occurrences, delivered outbox rows, and resolved events that
    have no occurrences left. Returns (occurrences_deleted, events_deleted)."""
    occ = conn.execute(
        "DELETE FROM error_occurrences WHERE occurred_utc < now() - %s RETURNING occurrence_id",
        (timedelta(days=occurrences_days),),
    ).fetchall()
    conn.execute(
        """
        DELETE FROM alert_outbox
        WHERE delivered_utc IS NOT NULL AND delivered_utc < now() - %s
        """,
        (timedelta(days=occurrences_days),),
    )
    events = conn.execute(
        """
        DELETE FROM error_events e
        WHERE e.state = 'resolved' AND e.last_seen_utc < now() - %s
          AND NOT EXISTS (SELECT 1 FROM error_occurrences o WHERE o.fingerprint = e.fingerprint)
        RETURNING e.event_id
        """,
        (timedelta(days=events_days),),
    ).fetchall()
    return len(occ), len(events)
