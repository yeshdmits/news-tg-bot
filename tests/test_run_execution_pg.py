"""Serverless job-execution tests: the advisory lock, gated batches across
executions, cron-quantised scheduling and crash hygiene."""

import httpx
import pytest

from archive.db import connect
from newsbot.runner import FETCH_LOCK_NAME, Deps, run_execution
from newsbot.telegram import TelegramClient
from newsbot.translate import NoneProvider, TranslationService
from tests.test_pipeline_pg import (
    CUSTOM_URL_MAP,
    FakeTelegramAPI,
    custom_spec,
    reset_fetch_timers,
    write_test_spec,
)

PING_URL = "https://ping.test/deadman"


def make_exec_deps(db, loaded, url_map):
    """Like test_pipeline_pg.make_deps, plus a recorded deadman endpoint."""
    telegram_api = FakeTelegramAPI()
    pings: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == PING_URL:
            pings.append(url)
            return httpx.Response(200, content=b"ok")
        body = url_map.get(url)
        if body is None:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=body)

    deps = Deps(
        loaded=loaded,
        conn=db,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        telegram=TelegramClient(
            "token", httpx.AsyncClient(transport=telegram_api.transport()), dry_run=False
        ),
        translator=TranslationService(db, NoneProvider()),
        base_dir=".",
        healthcheck_url=PING_URL,
    )
    return deps, telegram_api, pings


async def test_second_execution_skips_when_locked(db, pg_dsn, tmp_path):
    """The loser of the advisory-lock race exits clean — no fetches, no
    posts, no deadman ping (a skipped execution must look silent)."""
    loaded = write_test_spec(tmp_path, custom_spec())
    deps, telegram, pings = make_exec_deps(db, loaded, CUSTOM_URL_MAP)

    other = connect(pg_dsn)
    try:
        assert other.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (FETCH_LOCK_NAME,)
        ).fetchone()["ok"]
        result = await run_execution(deps)
    finally:
        other.close()  # releases the advisory lock with the session

    assert result.acquired is False
    assert result.stats is None
    assert telegram.calls == []
    assert pings == []
    for table in ("fetches", "items", "deliveries"):
        assert db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


async def test_feed_error_does_not_fail_execution(db, tmp_path):
    """One dead feed is normal operation — the other source still fetches,
    the execution completes and pings the deadman."""
    loaded = write_test_spec(tmp_path, custom_spec())
    url_map = {k: v for k, v in CUSTOM_URL_MAP.items() if "swissinfo" in k}  # blick 404s
    deps, telegram, pings = make_exec_deps(db, loaded, url_map)

    result = await run_execution(deps)

    assert result.acquired is True
    assert result.stats.errors  # blick raised
    assert result.stats.new_items > 0  # swissinfo stored anyway
    assert pings == [PING_URL]  # completed run → exactly one ping
    failures = db.execute(
        "SELECT consecutive_failures FROM source_state WHERE source_name = 'blick'"
    ).fetchone()
    assert failures["consecutive_failures"] == 1


async def test_batch_cap_and_next_batch_gated(db, tmp_path):
    """A batch never exceeds max_posts_per_run; inside the posting window the
    skipped candidates are recorded gated-rate_limited and post in the next
    window, flipping back to routed."""
    spec_dict = custom_spec(queue_policy="drain_all", post_interval_min=30, max_posts_per_run=2)
    spec_dict["sources"][0]["cold_start_policy"] = "post_newest:10"
    loaded = write_test_spec(tmp_path, spec_dict)
    deps, telegram, pings = make_exec_deps(db, loaded, CUSTOM_URL_MAP)

    first = await run_execution(deps)
    assert first.stats.posted == 2

    # immediately again: window closed → nothing posts, backlog marked gated
    reset_fetch_timers(db)
    second = await run_execution(deps)
    assert second.stats.posted == 0
    assert len(telegram.calls) == 2
    gated = db.execute(
        "SELECT count(*) AS n FROM routing_decisions "
        "WHERE decision = 'rate_limited' AND reason LIKE 'gated:%'"
    ).fetchone()["n"]
    assert gated == 8  # 10 eligible − 2 posted

    # next window: the gated backlog posts, capped again, decisions healed
    db.execute("UPDATE channel_state SET last_post_utc = now() - interval '31 minutes'")
    reset_fetch_timers(db)
    third = await run_execution(deps)
    assert third.stats.posted == 2
    assert len(telegram.calls) == 4
    posted_decisions = db.execute(
        """
        SELECT rd.decision FROM deliveries d
        JOIN routing_decisions rd
          ON rd.item_id = d.item_id AND rd.channel_name = d.channel_name
        WHERE d.status = 'sent'
        """
    ).fetchall()
    assert all(r["decision"] == "routed" for r in posted_decisions)


async def test_not_due_source_is_skipped(db, tmp_path):
    """next_fetch_utc is the only scheduling state — a source
    fetched at interval 20 is not touched by executions inside the interval
    and is fetched by the first execution at/after it (cron adds ≤5 min)."""
    spec_dict = custom_spec()
    spec_dict["sources"] = [dict(spec_dict["sources"][0], fetch_interval_min=20)]
    loaded = write_test_spec(tmp_path, spec_dict)
    deps, telegram, pings = make_exec_deps(db, loaded, CUSTOM_URL_MAP)

    await run_execution(deps)
    assert db.execute("SELECT count(*) AS n FROM fetches").fetchone()["n"] == 1
    due = db.execute(
        # ±1 min tolerance: next_fetch_utc is stamped from the host clock,
        # now() from the server's — sub-second skew must not flip the assert
        "SELECT next_fetch_utc > now() + interval '19 minutes' AS later,"
        "       next_fetch_utc <= now() + interval '21 minutes' AS bounded "
        "FROM source_state WHERE source_name = 'swissinfo'"
    ).fetchone()
    assert due["later"] and due["bounded"]

    # a 5-minutes-later execution finds nothing due
    await run_execution(deps)
    assert db.execute("SELECT count(*) AS n FROM fetches").fetchone()["n"] == 1

    # first execution at/after the due time fetches again
    db.execute("UPDATE source_state SET next_fetch_utc = now()")
    await run_execution(deps)
    assert db.execute("SELECT count(*) AS n FROM fetches").fetchone()["n"] == 2


async def test_failing_source_backs_off(db, tmp_path):
    """Consecutive failures double the retry interval, capped at 60 minutes —
    a dead feed is not retried every execution forever."""
    spec_dict = custom_spec()
    spec_dict["sources"] = [dict(spec_dict["sources"][0], fetch_interval_min=10)]
    loaded = write_test_spec(tmp_path, spec_dict)
    deps, telegram, pings = make_exec_deps(db, loaded, {})  # every fetch 404s

    expected_minutes = [10, 20, 40, 60, 60]  # 10·2^(n−1) capped at 60
    for n, minutes in enumerate(expected_minutes, start=1):
        reset_fetch_timers(db)
        await run_execution(deps)
        row = db.execute(
            # ±1 min tolerance around the expected backoff — host-vs-server
            # clock skew must not flip the bound (see test above)
            """
            SELECT consecutive_failures,
                   next_fetch_utc > now() + %s * interval '1 minute' AS later,
                   next_fetch_utc <= now() + %s * interval '1 minute' AS bounded
            FROM source_state WHERE source_name = 'swissinfo'
            """,
            (minutes - 1, minutes + 1),
        ).fetchone()
        assert row["consecutive_failures"] == n
        assert row["later"] and row["bounded"], f"failure {n}: expected ~{minutes} min"


async def test_lock_released_and_pool_closed_on_crash(db, pg_dsn, tmp_path, monkeypatch):
    """An exception inside the run releases the advisory lock (the finally
    path), so the next execution is not locked out."""
    loaded = write_test_spec(tmp_path, custom_spec())
    deps, telegram, pings = make_exec_deps(db, loaded, CUSTOM_URL_MAP)

    async def boom(_deps):
        raise RuntimeError("mid-run crash")

    monkeypatch.setattr("newsbot.runner.run_once", boom)
    with pytest.raises(RuntimeError):
        await run_execution(deps)
    assert pings == []  # a crashed run must look silent to the monitor

    other = connect(pg_dsn)
    try:
        row = other.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (FETCH_LOCK_NAME,)
        ).fetchone()
        assert row["ok"], "advisory lock leaked after a crashing execution"
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (FETCH_LOCK_NAME,))
    finally:
        other.close()
