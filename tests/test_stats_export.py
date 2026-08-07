"""Stats and export queries against a real Postgres."""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from archive import writer
from archive.db import transaction
from archive.models import Decision, DeliveryStatus
from archive.stats import (
    EXPORT_COLUMNS,
    backlog_sizes,
    channel_stats,
    delivery_stats,
    export_rows,
    source_stats,
    write_csv,
)
from feedspec.loader import load_spec
from tests.conftest import SPEC_FIXTURE
from tests.test_archive_writer import make_item, new_fetch

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _window() -> tuple[datetime, datetime]:
    """Around the real clock: first_seen_utc is stamped by the database at
    insert time, so a frozen window silently expires."""
    now = datetime.now(UTC)
    return now - timedelta(days=1), now + timedelta(days=1)


@pytest.fixture
def seeded(db):
    loaded = load_spec(SPEC_FIXTURE)
    writer.ensure_spec_version(db, loaded.spec_hash, loaded.raw)
    fetch_id = new_fetch(db, loaded.spec_hash, item_count=3)
    with transaction(db):
        id_a, _ = writer.store_item(
            db, fetch_id, make_item(source_item_id="a", categories=("pr",)),
            loaded.spec_hash, None,
        )
        id_b, _ = writer.store_item(
            db, fetch_id,
            make_item(source_item_id="b", url="https://example.org/b", title="B"),
            loaded.spec_hash, None,
        )
        id_c, _ = writer.store_item(
            db, fetch_id,
            make_item(source_item_id="c", url="https://example.org/c", title="C"),
            loaded.spec_hash, id_a,
        )
        writer.record_routing_decision(db, id_a, "news-ch", Decision.ROUTED)
        writer.record_routing_decision(db, id_b, "news-ch", Decision.FILTERED_CATEGORY,
                                       reason="excluded by category 'sport'")
        writer.record_routing_decision(db, id_c, "news-ch", Decision.DUPLICATE)
        writer.record_delivery(
            db, item_id=id_a, channel_name="news-ch", chat_id="-1001",
            status=DeliveryStatus.SENT, telegram_message_id=1, posted_utc=NOW,
        )
    return {"a": id_a, "b": id_b, "c": id_c}


def test_export_one_row_per_decision(db, seeded):
    rows = list(export_rows(db, *_window()))
    assert len(rows) == 3
    by_id = {row["source_item_id"]: row for row in rows}
    assert by_id["a"]["decision"] == "routed"
    assert by_id["a"]["delivery_status"] == "sent"
    assert by_id["a"]["categories"] == "pr"
    # filtered_category is not retained since migration 0004: the item still
    # exports, but its outcome lives in routing_stats as a per-day count and is
    # deliberately not joinable back to the item.
    assert by_id["b"]["decision"] is None
    assert by_id["b"]["delivery_status"] is None
    assert by_id["c"]["duplicate_of"] == seeded["a"]
    assert len(by_id["a"]["content_hash_hex"]) == 64


def test_export_keeps_the_item_when_its_decision_became_a_counter(db, seeded):
    """The item must not vanish from the export just because its outcome is no
    longer per-item — 'store everything, export everything' still holds."""
    rows = {r["source_item_id"]: r for r in export_rows(db, *_window())}
    assert set(rows) == {"a", "b", "c"}
    assert rows["b"]["title"]
    assert rows["b"]["canonical_url"]

    counted = db.execute(
        "SELECT sum(n) AS n FROM routing_stats WHERE decision = 'filtered_category'"
    ).fetchone()["n"]
    assert counted == 1


def test_export_window_filters(db, seeded):
    rows = list(export_rows(db, NOW - timedelta(days=10), NOW - timedelta(days=9)))
    assert rows == []


def test_export_csv_shape(db, seeded):
    out = io.StringIO()
    count = write_csv(export_rows(db, *_window()), out)
    assert count == 3
    parsed = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert len(parsed) == 3
    assert list(parsed[0].keys()) == EXPORT_COLUMNS


def test_stats_queries(db, seeded):
    sources = {row["source_name"]: row for row in source_stats(db)}
    assert sources["swissinfo"]["items"] == 3
    assert sources["swissinfo"]["duplicates"] == 1

    decisions = {(r["channel_name"], r["decision"]): r["n"] for r in channel_stats(db)}
    assert decisions[("news-ch", "routed")] == 1
    assert decisions[("news-ch", "duplicate")] == 1

    deliveries = {(r["channel_name"], r["status"]): r["n"] for r in delivery_stats(db)}
    assert deliveries[("news-ch", "sent")] == 1

    assert backlog_sizes(db) == []  # the routed item was delivered
