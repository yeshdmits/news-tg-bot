"""End-to-end pipeline tests: run_once against a real Postgres, with feeds
served from fixtures via httpx.MockTransport and a fake Telegram API.

Covers per-channel routing decisions, re-run idempotency and cold-start
policies.
"""

import json
from pathlib import Path

import httpx
import pytest

from feedspec.loader import load_spec
from newsbot.runner import Deps, run_once
from newsbot.telegram import TelegramClient
from newsbot.translate import NoneProvider, TranslationService
from tests.conftest import SPEC_FIXTURE

FIXTURES = Path("tests/fixtures")


class FakeTelegramAPI:
    """Counts Bot API calls and answers like Telegram."""

    def __init__(self):
        self.calls: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.calls.append({"method": request.url.path.split("/")[-1], **payload})
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": len(self.calls)}}
            )

        return httpx.MockTransport(handler)


def feed_transport(url_map: dict[str, bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = url_map.get(str(request.url))
        if body is None:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=body, headers={"ETag": f'W/"{hash(body)}"'})

    return httpx.MockTransport(handler)


def make_deps(db, loaded, url_map, *, dry_run=False):
    telegram_api = FakeTelegramAPI()
    deps = Deps(
        loaded=loaded,
        conn=db,
        http=httpx.AsyncClient(transport=feed_transport(url_map)),
        telegram=TelegramClient(
            "token", httpx.AsyncClient(transport=telegram_api.transport()), dry_run=dry_run
        ),
        translator=TranslationService(db, NoneProvider()),
        base_dir=".",
    )
    return deps, telegram_api


def reset_fetch_timers(db):
    db.execute("UPDATE source_state SET next_fetch_utc = now()")


def write_test_spec(tmp_path, spec: dict) -> "LoadedSpec":
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return load_spec(path)


def custom_spec(**channel_overrides) -> dict:
    """One channel fed by the swissinfo and blick fixtures."""
    channel = {
        "name": "test-ch",
        "id": "-100500",
        "post_interval_min": 30,
        "max_posts_per_run": 2,
        "max_age_min": None,
        "queue_policy": "drop_oldest",
    }
    channel.update(channel_overrides)
    return {
        "version": 2,
        "channels": [channel],
        "sources": [
            {
                "name": "swissinfo",
                "url": "https://feeds.test/swissinfo",
                "language": "en",
                "url_verified": "2026-08-01",
                "cold_start_policy": "post_newest:3",
                "channels": [{"name": "test-ch"}],
                "mapping": {
                    "items": "channel/item",
                    "id": "guid",
                    "title": "title",
                    "lead": "description",
                    "url": "link",
                    "published": "pubDate",
                    "published_format": "rfc822",
                    "category": "category",
                },
            },
            {
                "name": "blick",
                "url": "https://feeds.test/blick",
                "language": "de",
                "url_verified": "2026-08-01",
                "cold_start_policy": "skip_all",
                "channels": [{"name": "test-ch"}],
                "mapping": {
                    "items": "channel/item",
                    "id": "guid",
                    "id_pattern": "id(\\d+)\\.html",
                    "title": "title",
                    "lead": "description",
                    "url": "link",
                    "published": "pubDate",
                    "published_format": "rfc822",
                },
            },
        ],
    }


CUSTOM_URL_MAP = {
    "https://feeds.test/swissinfo": (FIXTURES / "swissinfo.xml").read_bytes(),
    "https://feeds.test/blick": (FIXTURES / "blick.xml").read_bytes(),
}


async def test_real_spec_cold_start_skip_all(db):
    """Cold start with the repo spec: on first fetch everything is stored,
    nothing posts, and every item has exactly one decision row per bound
    channel. All enabled sources serve their fixtures."""
    loaded = load_spec(SPEC_FIXTURE)
    url_map = {
        s.url: (FIXTURES / f"{s.name}.xml").read_bytes()
        for s in loaded.spec.sources
        if (FIXTURES / f"{s.name}.xml").exists()
    }
    deps, telegram = make_deps(db, loaded, url_map)

    stats = await run_once(deps)
    assert telegram.calls == []  # cold start: skip_all everywhere
    assert stats.posted == 0
    assert stats.new_items > 0

    items = db.execute("SELECT count(*) AS n FROM items").fetchone()["n"]
    assert items == stats.new_items

    # Every item is decided exactly once per bound channel — each news source
    # binds exactly one channel, so decisions == items and no (item, channel)
    # pair is missing. Since migration 0004 a decision is persisted either as a
    # row (RETAINED_DECISIONS) or as a counter, so the count spans both: these
    # are all cold_start_skip, which is a counter.
    rows = db.execute("SELECT count(*) AS n FROM routing_decisions").fetchone()["n"]
    counted = db.execute("SELECT COALESCE(sum(n), 0) AS n FROM routing_stats").fetchone()["n"]
    assert rows + counted == items

    # A cold start is skip_all everywhere, so every outcome here is either
    # cold_start_skip (a counter) or duplicate (a retained row). Nothing else,
    # and nothing unaccounted for: the sum above is the "no (item, channel)
    # pair was missed" check that a per-item LEFT JOIN used to make, which is
    # no longer possible for outcomes that are only counted.
    assert rows == db.execute(
        "SELECT count(*) AS n FROM routing_decisions WHERE decision = 'duplicate'"
    ).fetchone()["n"]
    assert counted == db.execute(
        "SELECT COALESCE(sum(n), 0) AS n FROM routing_stats WHERE decision = 'cold_start_skip'"
    ).fetchone()["n"]
    assert db.execute("SELECT count(*) AS n FROM deliveries").fetchone()["n"] == 0

    # every enabled source fetched its fixture cleanly; disabled ones never ran
    from feedspec.resolve import resolve_source

    enabled = {
        s.name for s in loaded.spec.sources if resolve_source(loaded.spec, s).enabled
    }
    fetched = db.execute(
        "SELECT count(*) AS n FROM fetches WHERE error IS NULL"
    ).fetchone()["n"]
    assert fetched == len(enabled)
    for source in loaded.spec.sources:
        if source.name not in enabled:
            assert db.execute(
                "SELECT count(*) AS n FROM fetches WHERE source_name = %s",
                (source.name,),
            ).fetchone()["n"] == 0


async def test_run_twice_sends_nothing_second_time(db):
    """Re-run idempotency, strengthened: the second run re-fetches identical
    content (timers forced due) and still sends nothing new."""
    loaded = load_spec(SPEC_FIXTURE)
    url_map = {
        s.url: (FIXTURES / f"{s.name}.xml").read_bytes()
        for s in loaded.spec.sources
        if (FIXTURES / f"{s.name}.xml").exists()
    }
    deps, telegram = make_deps(db, loaded, url_map)

    first = await run_once(deps)
    reset_fetch_timers(db)
    second = await run_once(deps)

    assert telegram.calls == []
    assert second.posted == 0
    assert second.new_items == 0
    items = db.execute("SELECT count(*) AS n FROM items").fetchone()["n"]
    assert items == first.new_items  # nothing duplicated by the second pass


async def test_post_newest_cold_start_posts_capped(db, tmp_path):
    """Cold start with post_newest:N and the per-run cap live: the 3 newest
    swissinfo items survive cold start, the per-chat cap posts 2, drop_oldest
    flips the overflow to rate_limited; blick (skip_all) contributes nothing."""
    loaded = write_test_spec(tmp_path, custom_spec())
    deps, telegram = make_deps(db, loaded, CUSTOM_URL_MAP)

    stats = await run_once(deps)
    assert stats.posted == 2
    assert len(telegram.calls) == 2

    # Decision histogram after the first run. Since migration 0004 the
    # retained outcomes keep a per-item row and the rest are counters, so the
    # histogram is the union of the two.
    counts = {
        r["decision"]: r["n"]
        for r in db.execute(
            """
            SELECT decision, count(*) AS n FROM routing_decisions GROUP BY decision
            UNION ALL
            SELECT decision, sum(n) AS n FROM routing_stats GROUP BY decision
            """
        )
    }
    assert counts["rate_limited"] == 1  # 3 routed − cap 2
    assert counts.get("routed", 0) == 2
    assert counts["cold_start_skip"] > 0

    sent = db.execute(
        "SELECT count(*) AS n FROM deliveries WHERE status = 'sent'"
    ).fetchone()["n"]
    assert sent == 2
    assert db.execute("SELECT last_post_utc FROM channel_state").fetchone()[
        "last_post_utc"
    ] is not None

    # immediately after: the interval gate is closed and content is unchanged
    reset_fetch_timers(db)
    again = await run_once(deps)
    assert again.posted == 0
    assert len(telegram.calls) == 2


async def test_drain_all_backlog_drains_across_runs(db, tmp_path):
    # every swissinfo item cold-start eligible so a backlog builds
    spec_dict = custom_spec(
        queue_policy="drain_all", post_interval_min=0, max_posts_per_run=2
    )
    spec_dict["sources"][0]["cold_start_policy"] = "post_newest:10"
    loaded = write_test_spec(tmp_path, spec_dict)
    deps, telegram = make_deps(db, loaded, CUSTOM_URL_MAP)

    first = await run_once(deps)
    assert first.posted == 2
    backlog = db.execute(
        """
        SELECT count(*) AS n FROM routing_decisions rd
        LEFT JOIN deliveries d ON d.item_id = rd.item_id
        WHERE rd.decision = 'routed' AND d.item_id IS NULL
        """
    ).fetchone()["n"]
    assert backlog == 8  # 10 eligible − 2 posted, still routed, not dropped

    reset_fetch_timers(db)
    second = await run_once(deps)
    assert second.posted == 2  # the backlog drains without new fetch content
    assert len(telegram.calls) == 4

    # chronological drain: message order follows published_utc ascending
    posted = db.execute(
        """
        SELECT i.published_utc FROM deliveries d
        JOIN items i ON i.item_id = d.item_id
        ORDER BY d.telegram_message_id
        """
    ).fetchall()
    stamps = [r["published_utc"] for r in posted]
    assert stamps == sorted(stamps)


async def test_dry_run_records_skipped_and_stays_idempotent(db, tmp_path):
    """DRY_RUN writes deliveries as 'skipped' and advances channel state, so
    a rehearsal is exactly as idempotent as a real run."""
    loaded = write_test_spec(tmp_path, custom_spec())
    deps, telegram = make_deps(db, loaded, CUSTOM_URL_MAP, dry_run=True)

    stats = await run_once(deps)
    assert stats.posted == 2
    assert telegram.calls == []  # no HTTP at all in dry run

    skipped = db.execute(
        "SELECT count(*) AS n FROM deliveries WHERE status = 'skipped' AND error = 'dry_run'"
    ).fetchone()["n"]
    assert skipped == 2

    reset_fetch_timers(db)
    db.execute("UPDATE channel_state SET last_post_utc = last_post_utc - interval '1 hour'")
    again = await run_once(deps)
    assert again.posted == 0
    assert db.execute("SELECT count(*) AS n FROM deliveries").fetchone()["n"] == 2


async def test_archive_only_source_stores_but_never_routes(db, tmp_path):
    """A source with no bindings is fetched, parsed and archived like any
    other, but produces no routing decisions and nothing is posted for it —
    while a bound source in the same spec posts normally."""
    spec_dict = custom_spec()
    spec_dict["sources"][0]["channels"] = []            # swissinfo: archive-only
    spec_dict["sources"][0]["cold_start_policy"] = "skip_all"
    spec_dict["sources"][1]["cold_start_policy"] = "post_newest:2"  # blick still posts
    loaded = write_test_spec(tmp_path, spec_dict)
    deps, telegram = make_deps(db, loaded, CUSTOM_URL_MAP)

    stats = await run_once(deps)

    # archived: items and a fetch row exist for the unbound source
    assert db.execute(
        "SELECT count(*) AS n FROM items WHERE source_name = 'swissinfo'"
    ).fetchone()["n"] > 0
    assert db.execute(
        "SELECT count(*) AS n FROM fetches WHERE source_name = 'swissinfo'"
    ).fetchone()["n"] == 1

    # but never routed — not even a cold_start_skip or filtered decision
    assert db.execute(
        """
        SELECT count(*) AS n FROM routing_decisions rd
        JOIN items i ON i.item_id = rd.item_id
        WHERE i.source_name = 'swissinfo'
        """
    ).fetchone()["n"] == 0
    assert db.execute(
        """
        SELECT count(*) AS n FROM deliveries d
        JOIN items i ON i.item_id = d.item_id
        WHERE i.source_name = 'swissinfo'
        """
    ).fetchone()["n"] == 0

    # the bound source is unaffected
    assert stats.posted == 2
    assert len(telegram.calls) == 2
    posted_sources = {
        r["source_name"]
        for r in db.execute(
            "SELECT DISTINCT i.source_name FROM deliveries d JOIN items i USING (item_id)"
        )
    }
    assert posted_sources == {"blick"}


async def test_binding_removed_retires_stale_backlog_without_crashing(db, tmp_path):
    """Routing decisions outlive the spec that made them: the backlog is keyed
    by channel alone. Turning a bound source archive-only must retire its
    orphaned backlog terminally, not raise KeyError mid-post-phase."""
    spec_dict = custom_spec(
        queue_policy="drain_all", post_interval_min=0, max_posts_per_run=2
    )
    loaded = write_test_spec(tmp_path, spec_dict)
    deps, telegram = make_deps(db, loaded, CUSTOM_URL_MAP)

    first = await run_once(deps)
    assert first.posted == 2
    backlog = db.execute(
        """
        SELECT count(*) AS n FROM routing_decisions rd
        LEFT JOIN deliveries d ON d.item_id = rd.item_id
        WHERE rd.decision = 'routed' AND d.item_id IS NULL
        """
    ).fetchone()["n"]
    assert backlog == 1  # 3 cold-start eligible − 2 posted

    # same spec, swissinfo unbound — its backlog now has no binding to post under
    spec_dict["sources"][0]["channels"] = []
    spec_dict["sources"][0]["cold_start_policy"] = "skip_all"
    deps.loaded = write_test_spec(tmp_path, spec_dict)
    reset_fetch_timers(db)

    second = await run_once(deps)  # must not raise

    assert second.posted == 0
    assert len(telegram.calls) == 2  # nothing new sent
    # channel_disabled is not in RETAINED_DECISIONS, so retiring the stale
    # backlog entry moves it out of routing_decisions and into the counters —
    # the reason string is what is lost in that trade (docs/data-model.md).
    assert db.execute(
        "SELECT count(*) AS n FROM routing_decisions WHERE decision = 'channel_disabled'"
    ).fetchone()["n"] == 0
    assert db.execute(
        "SELECT COALESCE(sum(n), 0) AS n FROM routing_stats WHERE decision = 'channel_disabled'"
    ).fetchone()["n"] == 1

    # terminal, so the row is gone from the backlog for good
    reset_fetch_timers(db)
    third = await run_once(deps)
    assert third.posted == 0
    assert db.execute(
        """
        SELECT count(*) AS n FROM routing_decisions rd
        LEFT JOIN deliveries d ON d.item_id = rd.item_id
        WHERE rd.decision = 'routed' AND d.item_id IS NULL
        """
    ).fetchone()["n"] == 0
