"""Archive writer tests against a real Postgres (marked ``pg`` automatically)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from lxml import etree

from archive import writer
from archive.db import transaction
from archive.dedupe import canonical_url, content_hash, find_duplicate, simhash64
from archive.models import ChannelState, Decision, DeliveryStatus, SourceState
from feedspec.loader import load_spec
from feedspec.resolve import resolve_source
from fetcher.parse import RawItem, parse_feed
from tests.conftest import SPEC_FIXTURE

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def loaded_spec():
    return load_spec(SPEC_FIXTURE)


@pytest.fixture
def spec_hash(db, loaded_spec):
    writer.ensure_spec_version(db, loaded_spec.spec_hash, loaded_spec.raw)
    return loaded_spec.spec_hash


def fixture_items(loaded_spec, name: str) -> list[RawItem]:
    spec = loaded_spec.spec
    source = next(s for s in spec.sources if s.name == name)
    body = Path(f"tests/fixtures/{name}.xml").read_bytes()
    return parse_feed(body, resolve_source(spec, source))


def new_fetch(db, spec_hash, source_name="swissinfo", **kwargs) -> "UUID":
    defaults = dict(
        source_name=source_name,
        started_utc=NOW,
        finished_utc=NOW,
        http_status=200,
        etag=None,
        last_modified=None,
        item_count=0,
        new_item_count=0,
        possible_gap=False,
        spec_hash=spec_hash,
    )
    defaults.update(kwargs)
    return writer.record_fetch(db, **defaults)


def make_item(**overrides) -> RawItem:
    values = dict(
        source_name="swissinfo",
        source_item_id="id-1",
        title="A title",
        url="https://example.org/a",
        lead="A lead",
        language="en",
        image_url=None,
        categories=(),
        published_raw="Sat, 1 Aug 2026 11:24:00 GMT",
        published_utc=datetime(2026, 8, 1, 11, 24, tzinfo=timezone.utc),
        copyright_holder=None,
        raw_xml=b"<item><title>A title</title></item>",
    )
    values.update(overrides)
    return RawItem(**values)


def test_every_fixture_item_produces_exactly_one_row(db, loaded_spec, spec_hash):
    """Every fetched item → exactly one items row; re-storing the same fetch
    content is idempotent (crash-retry safety)."""
    items = fixture_items(loaded_spec, "swissinfo")
    fetch_id = new_fetch(db, spec_hash, item_count=len(items))

    with transaction(db):
        results = [writer.store_item(db, fetch_id, item, spec_hash, None) for item in items]
    assert all(is_new for _, is_new in results)

    count = db.execute("SELECT count(*) AS n FROM items").fetchone()["n"]
    assert count == len(items)

    # second identical fetch: same rows, nothing new
    fetch_id_2 = new_fetch(db, spec_hash)
    with transaction(db):
        results_2 = [writer.store_item(db, fetch_id_2, item, spec_hash, None) for item in items]
    assert not any(is_new for _, is_new in results_2)
    assert db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == count
    # and the ids resolve to the originals
    assert {r[0] for r in results_2} == {r[0] for r in results}


def test_raw_xml_round_trips(db, loaded_spec, spec_hash):
    """Re-parsing a stored raw_xml blob with the same mapping yields
    identical parsed fields (field equality, not byte equality)."""
    items = fixture_items(loaded_spec, "swissinfo")
    fetch_id = new_fetch(db, spec_hash)
    with transaction(db):
        stored = {
            writer.store_item(db, fetch_id, item, spec_hash, None)[0]: item
            for item in items
        }

    from fetcher.mapping import CompiledMapping

    doc_root = etree.fromstring(Path("tests/fixtures/swissinfo.xml").read_bytes())
    compiled = CompiledMapping({k: v for k, v in doc_root.nsmap.items() if k})
    for item_id, original in stored.items():
        blob = db.execute(
            "SELECT raw_xml FROM items WHERE item_id = %s", (item_id,)
        ).fetchone()["raw_xml"]
        element = etree.fromstring(bytes(blob))
        assert compiled.eval_first(element, "title") == original.title
        assert compiled.eval_first(element, "link") == original.url
        assert compiled.eval_first(element, "guid") == original.source_item_id


def test_all_timestamp_columns_are_timestamptz(db):
    """No naive timestamp columns anywhere; the session TZ is UTC."""
    rows = db.execute(
        """
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type LIKE 'timestamp%'
        """
    ).fetchall()
    assert rows, "expected timestamp columns"
    naive = [r for r in rows if r["data_type"] != "timestamp with time zone"]
    assert naive == []

    tz = db.execute("SHOW timezone").fetchone()["TimeZone"]
    assert tz == "UTC"


def test_timestamps_round_trip_aware(db, loaded_spec, spec_hash):
    fetch_id = new_fetch(db, spec_hash)
    with transaction(db):
        item_id, _ = writer.store_item(db, fetch_id, make_item(), spec_hash, None)
    row = db.execute(
        "SELECT published_utc, first_seen_utc FROM items WHERE item_id = %s", (item_id,)
    ).fetchone()
    assert row["published_utc"].tzinfo is not None
    assert row["published_utc"] == datetime(2026, 8, 1, 11, 24, tzinfo=timezone.utc)
    assert row["first_seen_utc"].tzinfo is not None


def test_tracking_param_variants_deduplicate(db, loaded_spec, spec_hash):
    """Dedupe against the database: two items whose URLs differ only by
    tracking params share a content_hash and the second becomes a duplicate."""
    fetch_id = new_fetch(db, spec_hash)
    first = make_item(
        source_item_id="a-1",
        url="https://example.org/story",
    )
    second = make_item(
        source_name="blick",
        source_item_id="b-1",
        url="https://example.org/story?utm_source=rss&linkType=rss",
    )
    with transaction(db):
        first_id, _ = writer.store_item(db, fetch_id, first, spec_hash, None)

    canon = canonical_url(second.url)
    dup_of = find_duplicate(db, content_hash(canon, second.title), simhash64(second.title), NOW)
    assert dup_of == first_id

    with transaction(db):
        second_id, is_new = writer.store_item(db, fetch_id, second, spec_hash, dup_of)
    assert is_new  # duplicates are stored, never dropped
    row = db.execute(
        "SELECT duplicate_of FROM items WHERE item_id = %s", (second_id,)
    ).fetchone()
    assert row["duplicate_of"] == first_id


def test_near_duplicate_found_by_simhash(db, loaded_spec, spec_hash):
    """The fed-press-all / fed-monetary overlap: same headline, different URL.
    content_hash differs (URL is part of it) so only the simhash scan can
    collapse the pair."""
    fetch_id = new_fetch(db, spec_hash)
    original = make_item(
        source_item_id="fed-1",
        url="https://federalreserve.gov/press/a",
        title="Federal Reserve issues FOMC statement on interest rates",
    )
    with transaction(db):
        original_id, _ = writer.store_item(db, fetch_id, original, spec_hash, None)

    near_title = "Federal Reserve issues FOMC statement on interest rates!"
    near_url = canonical_url("https://www.federalreserve.gov/press/b")
    assert content_hash(near_url, near_title) != content_hash(
        canonical_url(original.url), original.title
    )
    dup_of = find_duplicate(db, content_hash(near_url, near_title), simhash64(near_title), NOW)
    assert dup_of == original_id


def test_dedupe_window_expires(db, loaded_spec, spec_hash):
    fetch_id = new_fetch(db, spec_hash)
    stale = make_item(source_item_id="old-1", url="https://example.org/story")
    with transaction(db):
        stale_id, _ = writer.store_item(db, fetch_id, stale, spec_hash, None)
    db.execute(
        "UPDATE items SET first_seen_utc = %s WHERE item_id = %s",
        (NOW - timedelta(hours=100), stale_id),
    )
    canon = canonical_url("https://example.org/story")
    assert find_duplicate(db, content_hash(canon, "A title"), simhash64("A title"), NOW) is None


def test_routing_decisions_idempotent(db, loaded_spec, spec_hash):
    fetch_id = new_fetch(db, spec_hash)
    with transaction(db):
        item_id, _ = writer.store_item(db, fetch_id, make_item(), spec_hash, None)
        writer.record_routing_decision(db, item_id, "news-ch", Decision.ROUTED)
        writer.record_routing_decision(db, item_id, "news-ch", Decision.COLD_START_SKIP)

    rows = db.execute("SELECT * FROM routing_decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["decision"] == "routed"  # first write wins; re-runs are no-ops

    writer.update_routing_decision(db, item_id, "news-ch", Decision.RATE_LIMITED, "over cap")
    row = db.execute("SELECT decision, reason FROM routing_decisions").fetchone()
    assert row["decision"] == "rate_limited"


def test_item_categories_ordered(db, loaded_spec, spec_hash):
    fetch_id = new_fetch(db, spec_hash)
    item = make_item(categories=("Politics", "Europe", "Economy"))
    with transaction(db):
        item_id, _ = writer.store_item(db, fetch_id, item, spec_hash, None)
    record = writer.get_item(db, item_id)
    assert record.categories == ("Politics", "Europe", "Economy")


def test_source_state_round_trip(db):
    state = writer.load_source_state(db, "swissinfo", NOW)
    assert state.cold_start_done is False
    writer.save_source_state(
        db,
        SourceState(
            source_name="swissinfo",
            next_fetch_utc=NOW + timedelta(minutes=15),
            last_fetch_utc=NOW,
            etag='W/"abc"',
            cold_start_done=True,
        ),
    )
    reloaded = writer.load_source_state(db, "swissinfo", NOW)
    assert reloaded.cold_start_done is True
    assert reloaded.etag == 'W/"abc"'
    assert reloaded.next_fetch_utc == NOW + timedelta(minutes=15)


def test_channel_state_round_trip(db):
    assert writer.load_channel_state(db, "news-ch").last_post_utc is None
    writer.save_channel_state(db, ChannelState(channel_name="news-ch", last_post_utc=NOW))
    assert writer.load_channel_state(db, "news-ch").last_post_utc == NOW


def test_postable_backlog_excludes_delivered(db, loaded_spec, spec_hash):
    fetch_id = new_fetch(db, spec_hash)
    with transaction(db):
        id_a, _ = writer.store_item(db, fetch_id, make_item(source_item_id="a"), spec_hash, None)
        id_b, _ = writer.store_item(
            db, fetch_id, make_item(source_item_id="b", url="https://example.org/b", title="B"),
            spec_hash, None,
        )
        writer.record_routing_decision(db, id_a, "news-ch", Decision.ROUTED)
        writer.record_routing_decision(db, id_b, "news-ch", Decision.ROUTED)

    assert {r.item_id for r in writer.get_postable_backlog(db, "news-ch")} == {id_a, id_b}

    writer.record_delivery(
        db, item_id=id_a, channel_name="news-ch", chat_id="-1001",
        status=DeliveryStatus.SENT, telegram_message_id=5, posted_utc=NOW,
    )
    assert {r.item_id for r in writer.get_postable_backlog(db, "news-ch")} == {id_b}


def test_translation_cache_round_trip(db):
    chash = content_hash("https://example.org/a", "A title")
    assert writer.get_cached_translation(db, chash, "lead", "en") is None
    writer.store_translation(
        db, chash=chash, field="lead", target_language="en",
        translated="Hello", provider="deepl",
    )
    assert writer.get_cached_translation(db, chash, "lead", "en") == "Hello"
    # idempotent re-store keeps the first value
    writer.store_translation(
        db, chash=chash, field="lead", target_language="en",
        translated="Other", provider="deepl",
    )
    assert writer.get_cached_translation(db, chash, "lead", "en") == "Hello"
