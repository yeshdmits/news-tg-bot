"""item_keys: the dedupe guarantee that outlives the item.

The property that matters most here is the one Phase 4 breaks if it is wrong.
Once `items` is partitioned, its `UNIQUE (source_name, source_item_id)` is
illegal — adding the partition key to it would compile, migrate cleanly, and
silently let the same article re-ingest into a later partition. `item_keys` is
where that guarantee lives instead, and it has to work when the item's own row
is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from archive import retention, writer
from archive.dedupe import (
    canonical_url,
    content_hash,
    find_duplicate,
    simhash64,
)
from archive.ids import timestamp_of
from fetcher.parse import RawItem

SPEC_HASH = "d" * 64


def _raw(title: str, *, source="src-1", ident="a", url=None) -> RawItem:
    return RawItem(
        source_name=source,
        source_item_id=ident,
        raw_xml=b"<item/>",
        title=title,
        lead=None,
        language="en",
        url=url or f"https://{source}.example/{ident}",
        image_url=None,
        published_raw=None,
        published_utc=None,
        categories=[],
        copyright_holder=None,
    )


@pytest.fixture
def stored(db):
    """A spec version and a fetch row, so store_item has its foreign keys."""
    writer.ensure_spec_version(db, SPEC_HASH, {"version": 2})
    fetch_id = writer.record_fetch(
        db, source_name="src-1", started_utc=datetime.now(UTC), finished_utc=None,
        http_status=200, etag=None, last_modified=None, item_count=1,
        new_item_count=1, possible_gap=False, spec_hash=SPEC_HASH,
    )
    return fetch_id


def test_storing_an_item_writes_its_key(db, stored):
    item_id, is_new = writer.store_item(db, stored, _raw("Rates held steady"), SPEC_HASH, None)

    assert is_new
    key = db.execute(
        "SELECT * FROM item_keys WHERE source_name = 'src-1' AND source_item_id = 'a'"
    ).fetchone()
    assert key["item_id"] == item_id
    assert key["first_seen_utc"] == timestamp_of(item_id)


def test_dedupe_rejects_a_refetch_after_the_item_row_is_purged(db, stored):
    """The acceptance property for migration 0005.

    Simulates what Phase 4 does for real: the item's row is dropped with its
    partition while the key survives on its own longer TTL. A feed
    republishing the article must still be recognised.
    """
    item = _raw("Central bank holds interest rates")
    item_id, is_new = writer.store_item(db, stored, item, SPEC_HASH, None)
    assert is_new

    # The partition drop, in miniature.
    db.execute("DELETE FROM items WHERE item_id = %s", (item_id,))
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM item_keys").fetchone()["n"] == 1

    # Same feed, same item, fetched again.
    seen = db.execute(
        "SELECT item_id FROM item_keys WHERE source_name = %s AND source_item_id = %s",
        (item.source_name, item.source_item_id),
    ).fetchone()
    assert seen is not None, "the article would re-ingest as new"
    assert seen["item_id"] == item_id

    _, is_new_again = writer.store_item(db, stored, item, SPEC_HASH, None)
    assert not is_new_again
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 0


def test_exact_duplicate_is_found_through_item_keys(db, stored):
    first_id, _ = writer.store_item(db, stored, _raw("Grid operator raises capacity"), SPEC_HASH, None)

    canon = canonical_url("https://src-1.example/a")
    found = find_duplicate(
        db, content_hash(canon, "Grid operator raises capacity"),
        simhash64("Grid operator raises capacity"), datetime.now(UTC),
    )
    assert found == first_id


def test_verbatim_republication_is_found_by_the_band_probe(db, stored):
    """Another outlet running the agency headline unchanged: different URL, so
    content_hash misses it, and the simhash branch has to catch it."""
    title = "Statistics office publishes the quarterly outlook"
    first_id, _ = writer.store_item(db, stored, _raw(title), SPEC_HASH, None)

    other_url = "https://src-2.example/z"
    found = find_duplicate(
        db, content_hash(canonical_url(other_url), title), simhash64(title), datetime.now(UTC)
    )
    assert found == first_id


def test_duplicates_are_not_candidates_so_chains_stay_flat(db, stored):
    title = "Trade body defends border checks"
    original_id, _ = writer.store_item(db, stored, _raw(title, ident="a"), SPEC_HASH, None)
    writer.store_item(db, stored, _raw(title, ident="b", url="https://src-1.example/b"),
                      SPEC_HASH, original_id)

    found = find_duplicate(
        db, content_hash(canonical_url("https://src-3.example/c"), title),
        simhash64(title), datetime.now(UTC),
    )
    assert found == original_id, "a duplicate was offered as the attribution target"


def test_unrelated_titles_are_not_matched(db, stored):
    writer.store_item(db, stored, _raw("Energy regulator reviews the emissions cap"), SPEC_HASH, None)

    other = "Health service postpones rail investment after a sharp revision"
    found = find_duplicate(
        db, content_hash(canonical_url("https://src-9.example/x"), other),
        simhash64(other), datetime.now(UTC),
    )
    assert found is None


def test_a_key_outside_the_dedupe_window_is_not_matched(db, stored):
    """item_keys outlives the items row, but the 72 h dedupe window still
    bounds what counts as the same story."""
    title = "Port authority confirms licence conditions"
    writer.store_item(db, stored, _raw(title), SPEC_HASH, None)
    db.execute("UPDATE item_keys SET first_seen_utc = now() - interval '100 hours'")

    found = find_duplicate(
        db, content_hash(canonical_url("https://src-4.example/y"), title),
        simhash64(title), datetime.now(UTC),
    )
    assert found is None


def test_ttl_purge_removes_old_keys_in_chunks(db, stored):
    writer.store_item(db, stored, _raw("Old story", ident="old"), SPEC_HASH, None)
    writer.store_item(db, stored, _raw("New story", ident="new"), SPEC_HASH, None)
    db.execute(
        "UPDATE item_keys SET first_seen_utc = now() - interval '40 days' "
        "WHERE source_item_id = 'old'"
    )

    removed = retention.purge_item_keys(db, days=30)

    assert removed == 1
    surviving = db.execute("SELECT source_item_id FROM item_keys").fetchall()
    assert [r["source_item_id"] for r in surviving] == ["new"]


def test_past_the_ttl_a_republished_article_ingests_as_new(db, stored):
    """The documented tradeoff, asserted rather than assumed."""
    item = _raw("Labour union questions wage guidance")
    first_id, _ = writer.store_item(db, stored, item, SPEC_HASH, None)
    db.execute("DELETE FROM items WHERE item_id = %s", (first_id,))
    db.execute("UPDATE item_keys SET first_seen_utc = now() - interval '40 days'")

    retention.purge_item_keys(db, days=30)

    seen = db.execute(
        "SELECT item_id FROM item_keys WHERE source_name = %s AND source_item_id = %s",
        (item.source_name, item.source_item_id),
    ).fetchone()
    assert seen is None, (
        "the key survived its TTL; this test documents that it does NOT, and "
        "that re-ingestion past the window is the accepted price"
    )
