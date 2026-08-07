"""Read-only archive statistics and export queries.

Lives in archive because it is pure SQL over archive tables; cli.py renders.
"""

from __future__ import annotations

import csv
from datetime import datetime
from typing import Any, Iterator, TextIO

import psycopg

EXPORT_COLUMNS = [
    "item_id", "source_name", "source_item_id", "title", "lead_original",
    "lead_language", "url_raw", "canonical_url", "image_url", "published_raw",
    "published_utc", "first_seen_utc", "content_hash_hex", "title_simhash",
    "duplicate_of", "copyright_holder", "item_spec_hash", "categories",
    "channel_name", "decision", "decision_reason", "matched_clause",
    "decided_utc", "delivery_status", "telegram_message_id", "posted_utc",
    "delivery_attempt", "delivery_error",
]

_EXPORT_SQL = """
SELECT
  i.item_id, i.source_name, i.source_item_id, i.title, i.lead_original,
  i.lead_language, i.url_raw, i.canonical_url, i.image_url, i.published_raw,
  i.published_utc, i.first_seen_utc, encode(i.content_hash, 'hex') AS content_hash_hex,
  i.title_simhash, i.duplicate_of, i.copyright_holder, i.spec_hash AS item_spec_hash,
  (SELECT string_agg(c.category, '|' ORDER BY c.ordinal)
     FROM item_categories c WHERE c.item_id = i.item_id) AS categories,
  rd.channel_name, rd.decision::text AS decision, rd.reason AS decision_reason,
  rd.matched_clause::text AS matched_clause, rd.decided_utc,
  d.status::text AS delivery_status, d.telegram_message_id, d.posted_utc,
  d.attempt AS delivery_attempt, d.error AS delivery_error
FROM items i
LEFT JOIN routing_decisions rd ON rd.item_id = i.item_id
LEFT JOIN deliveries d
  ON d.item_id = rd.item_id AND d.channel_name = rd.channel_name
WHERE i.first_seen_utc >= %s AND i.first_seen_utc < %s
ORDER BY i.first_seen_utc, i.item_id, rd.channel_name
"""


def export_rows(
    conn: psycopg.Connection, from_utc: datetime, to_utc: datetime
) -> Iterator[dict[str, Any]]:
    """One row per (item, routing decision); undecided items still export
    (store everything, export everything).

    Since migration 0004 a NULL ``decision`` means one of two things, and this
    view cannot tell them apart: the item genuinely had no bound channel
    (archive-only source), or its outcome was one of the negative ones that is
    now persisted as a counter in ``routing_stats`` rather than a per-item row.
    Only ``routed``, ``rate_limited`` and ``duplicate`` still carry a decision
    here. Per-day totals for the rest come from ``routing_stats``; they are not
    joinable back to individual items, by design — see docs/data-model.md.
    """
    with conn.cursor() as cursor:
        cursor.execute(_EXPORT_SQL, (from_utc, to_utc))
        for row in cursor:
            yield row


def write_csv(rows: Iterator[dict[str, Any]], out: TextIO) -> int:
    """Stream export rows to CSV with the standard header; returns row count."""
    dict_writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS)
    dict_writer.writeheader()
    count = 0
    for row in rows:
        dict_writer.writerow(row)
        count += 1
    return count


def write_parquet(rows: Iterator[dict[str, Any]], path: str) -> int:
    """Write export rows to a Parquet file; returns row count. Needs pyarrow
    (the optional export extra) and exits with an install hint without it;
    UUID columns are stringified for Arrow."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise SystemExit(
            "parquet export needs pyarrow: pip install '.[export]'"
        ) from e

    materialised = list(rows)
    columns = {
        name: [
            str(row[name]) if name in ("item_id", "duplicate_of") and row[name] else row[name]
            for row in materialised
        ]
        for name in EXPORT_COLUMNS
    }
    pq.write_table(pa.table(columns), path)
    return len(materialised)


def source_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-source totals: items, duplicates, items/day, fetch gaps and errors,
    last fetch time."""
    return conn.execute(
        """
        SELECT
          s.source_name,
          count(i.item_id) AS items,
          count(i.duplicate_of) AS duplicates,
          round(count(i.item_id)::numeric
                / greatest(count(DISTINCT date_trunc('day', i.first_seen_utc)), 1), 1)
            AS items_per_day,
          f.gaps,
          f.fetch_errors,
          f.last_fetch_utc
        FROM (SELECT DISTINCT source_name FROM fetches) s
        LEFT JOIN items i ON i.source_name = s.source_name
        LEFT JOIN LATERAL (
          SELECT count(*) FILTER (WHERE possible_gap) AS gaps,
                 count(*) FILTER (WHERE error IS NOT NULL) AS fetch_errors,
                 max(started_utc) AS last_fetch_utc
          FROM fetches WHERE source_name = s.source_name
        ) f ON true
        GROUP BY s.source_name, f.gaps, f.fetch_errors, f.last_fetch_utc
        ORDER BY s.source_name
        """
    ).fetchall()


def channel_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Routing decision counts per (channel, decision)."""
    return conn.execute(
        """
        SELECT channel_name, decision::text AS decision, count(*) AS n
        FROM routing_decisions
        GROUP BY channel_name, decision
        ORDER BY channel_name, decision
        """
    ).fetchall()


def delivery_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Delivery counts per (channel, status)."""
    return conn.execute(
        """
        SELECT channel_name, status::text AS status, count(*) AS n
        FROM deliveries
        GROUP BY channel_name, status
        ORDER BY channel_name, status
        """
    ).fetchall()


def backlog_sizes(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Routed-but-undelivered item count per channel."""
    return conn.execute(
        """
        SELECT rd.channel_name, count(*) AS n
        FROM routing_decisions rd
        LEFT JOIN deliveries d
          ON d.item_id = rd.item_id AND d.channel_name = rd.channel_name
        WHERE rd.decision = 'routed' AND d.item_id IS NULL
        GROUP BY rd.channel_name
        ORDER BY rd.channel_name
        """
    ).fetchall()
