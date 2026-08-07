"""Operational retention windows.

Every window is configurable and none is hardcoded at the call site. The
`fetches` purge additionally has an ordering dependency on migration 0006 —
shipping a purge that throws would be worse than shipping none.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from archive import retention
from archive.retention import RetentionWindows


def _seen(db, update_id: int, age_hours: float) -> None:
    db.execute(
        "INSERT INTO seen_updates (update_id, seen_utc) VALUES (%s, now() - %s)",
        (update_id, timedelta(hours=age_hours)),
    )


def test_seen_updates_purged_past_the_window(db):
    _seen(db, 1, 48)
    _seen(db, 2, 1)

    removed = retention.purge_seen_updates(db, hours=24)

    assert removed == 1
    surviving = db.execute("SELECT update_id FROM seen_updates").fetchall()
    assert [r["update_id"] for r in surviving] == [2]


def test_seen_updates_window_is_configurable(db):
    """The old implementation hardcoded 24 h in a default argument."""
    _seen(db, 1, 5)
    _seen(db, 2, 1)

    assert retention.purge_seen_updates(db, hours=2) == 1
    assert retention.purge_seen_updates(db, hours=2) == 0


def test_fetches_purge_is_inert_while_the_items_foreign_key_exists(db):
    """items.fetch_id references fetches until migration 0006. Deleting inside
    the window would fail on rows still referenced, so this returns 0 rather
    than raising — the dependency is recorded in docs/data-model.md."""
    db.execute(
        "INSERT INTO spec_versions (spec_hash, spec) VALUES ('c', '{}') ON CONFLICT DO NOTHING"
    )
    db.execute(
        "INSERT INTO fetches (source_name, started_utc, spec_hash) "
        "VALUES ('src', now() - interval '200 days', 'c')"
    )

    assert retention.purge_fetches(db, days=90) == 0
    assert db.execute("SELECT count(*) AS n FROM fetches").fetchone()["n"] == 1


def test_run_reports_what_each_table_lost(db):
    _seen(db, 1, 48)

    removed = retention.run(db, RetentionWindows(seen_updates_ttl_hours=24))

    assert removed == {"seen_updates": 1, "fetches": 0}


def test_defaults_match_the_documented_windows():
    windows = RetentionWindows()
    assert windows.seen_updates_ttl_hours == 24
    assert windows.fetches_ttl_days == 90


@pytest.mark.parametrize(
    "env,field,expected",
    [
        ({"SEEN_UPDATES_TTL_HOURS": "6"}, "seen_updates_ttl_hours", 6),
        ({"FETCHES_TTL_DAYS": "30"}, "fetches_ttl_days", 30),
    ],
)
def test_windows_come_from_the_environment(monkeypatch, env, field, expected):
    from cli import Settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert getattr(Settings.from_env(), field) == expected


@pytest.mark.parametrize(
    "env", [{"SEEN_UPDATES_TTL_HOURS": "0"}, {"FETCHES_TTL_DAYS": "-1"},
            {"SEEN_UPDATES_TTL_HOURS": "soon"}]
)
def test_a_nonsense_window_is_rejected_at_startup(monkeypatch, env):
    """A zero or negative TTL would delete everything on the next pass."""
    from cli import Settings, SettingsError

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SettingsError):
        Settings.from_env()
