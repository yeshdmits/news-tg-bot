"""Migrations must survive a round trip against data that already exists.

Structures are the easy part; the risk is a migration that defines them
correctly and silently loses rows. These run alembic for real against a
database seeded with pre-migration data.

Honesty about downgrades is the point of the exemptions here. Each migration
declares what its downgrade restores:

* 0005 — a clean round trip (added when Phase 3 lands);
* 0004 — schema-complete but **lossy** for routing_decisions: the negative
  outcomes were aggregated into counters and deleted, and nothing can bring
  them back;
* 0006 — refuses outright (added when Phase 4 lands).
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config

from archive.db import connect

# Tables whose contents legitimately do not survive downgrade -1 from head,
# with the reason. Anything not listed must round-trip exactly.
LOSSY_ON_DOWNGRADE = {
    "routing_decisions": (
        "0004 aggregates the non-retained outcomes into routing_stats and "
        "deletes them; no downgrade can restore per-item rows"
    ),
}


def _alembic(dsn: str) -> Config:
    import os

    os.environ["DATABASE_URL"] = dsn
    return Config("alembic.ini")


def _counts(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT c.relname AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND c.relname <> 'alembic_version'
        """
    ).fetchall()
    return {
        r["name"]: conn.execute(f'SELECT count(*) AS n FROM "{r["name"]}"').fetchone()["n"]
        for r in rows
    }


def _seed_pre_migration(conn) -> uuid.UUID:
    """Rows in the shape migration 0004 has to migrate, not just tolerate."""
    spec_hash = "b" * 64
    conn.execute(
        "INSERT INTO spec_versions (spec_hash, spec) VALUES (%s, '{}') "
        "ON CONFLICT DO NOTHING",
        (spec_hash,),
    )
    fetch_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO fetches (fetch_id, source_name, started_utc, spec_hash) "
        "VALUES (%s, 'src', now(), %s)",
        (fetch_id, spec_hash),
    )
    item_id = uuid.uuid4()  # deliberately v4: this is a pre-migration row
    conn.execute(
        """
        INSERT INTO items (item_id, source_name, fetch_id, source_item_id, raw_xml,
                           title, url_raw, canonical_url, first_seen_utc,
                           content_hash, title_simhash, spec_hash)
        VALUES (%s, 'src', %s, 'x1', %s, 't', 'https://e.org/a', 'https://e.org/a',
                now(), %s, 1, %s)
        """,
        (item_id, fetch_id, b"<item/>", b"\x00" * 32, spec_hash),
    )
    for channel, decision in (
        ("ch-a", "routed"),
        ("ch-b", "filtered_category"),
        ("ch-c", "predicate_failed"),
    ):
        conn.execute(
            "INSERT INTO routing_decisions (item_id, channel_name, decision, decided_utc) "
            "VALUES (%s, %s, %s, now() - interval '1 hour')",
            (item_id, channel, decision),
        )
    return item_id


@pytest.fixture
def migrated(pg_dsn):
    """A connection at head, with the schema reset around the test so a failed
    round trip cannot strand the shared session database at the wrong rev."""
    conn = connect(pg_dsn)
    yield conn
    command.upgrade(_alembic(pg_dsn), "head")
    conn.close()


def test_0004_round_trips_and_declares_what_it_loses(migrated, pg_dsn):
    conn = migrated
    conn.execute("TRUNCATE routing_decisions, routing_stats, item_categories, "
                 "deliveries, items, legacy_item_index, fetches, spec_versions CASCADE")

    command.downgrade(_alembic(pg_dsn), "0003")
    item_id = _seed_pre_migration(conn)
    before = _counts(conn)

    command.upgrade(_alembic(pg_dsn), "0004")

    # The negative outcomes became counters; the retained one kept its row.
    assert conn.execute(
        "SELECT count(*) AS n FROM routing_decisions"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COALESCE(sum(n), 0) AS n FROM routing_stats"
    ).fetchone()["n"] == 2
    # The v4 item is indexed, or it would be permanently unlocatable.
    assert conn.execute(
        "SELECT archive_dt FROM legacy_item_index WHERE item_id = %s", (item_id,)
    ).fetchone() is not None

    command.downgrade(_alembic(pg_dsn), "0003")
    after = _counts(conn)

    for table, count in before.items():
        if table in LOSSY_ON_DOWNGRADE:
            continue
        assert after.get(table) == count, f"{table} did not survive the round trip"

    # And state the loss rather than quietly skipping it.
    assert after["routing_decisions"] == 1 < before["routing_decisions"] == 3, (
        LOSSY_ON_DOWNGRADE["routing_decisions"]
    )


def test_0004_restores_the_server_defaults_on_downgrade(migrated, pg_dsn):
    conn = migrated
    command.downgrade(_alembic(pg_dsn), "0003")

    rows = conn.execute(
        """
        SELECT column_name, column_default FROM information_schema.columns
        WHERE table_name = 'items' AND column_name IN ('item_id', 'first_seen_utc')
        """
    ).fetchall()
    defaults = {r["column_name"]: r["column_default"] for r in rows}
    assert defaults["item_id"] == "gen_random_uuid()"
    assert defaults["first_seen_utc"] == "now()"


def test_migration_lifts_the_role_statement_timeout(migrated, pg_dsn):
    """Migration 0003 pins statement_timeout to 30s on the role, and migrations
    run as that role. A backfill that does not lift it works on a 9 MB dev
    database and fails only in production, which is the worst place to find out.
    """
    conn = migrated
    role_default = conn.execute(
        "SELECT setconfig FROM pg_db_role_setting s "
        "JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = current_user"
    ).fetchone()
    assert role_default is not None
    assert any("statement_timeout=30s" in c.replace(" ", "") for c in role_default["setconfig"]), (
        "0003's role-level statement_timeout is gone; the migration guards "
        "assume it is there"
    )


def test_defaults_are_dropped_at_head(migrated):
    """With the writer always supplying both values, a server default is a trap
    for any insert path that forgets."""
    rows = migrated.execute(
        """
        SELECT column_name, column_default FROM information_schema.columns
        WHERE table_name = 'items' AND column_name IN ('item_id', 'first_seen_utc')
        """
    ).fetchall()
    assert {r["column_name"]: r["column_default"] for r in rows} == {
        "item_id": None,
        "first_seen_utc": None,
    }


def test_inserting_without_an_id_now_fails_loudly(migrated):
    """The point of dropping the default: a forgotten id is an error, not a
    silently non-derivable UUIDv4."""
    import psycopg

    with pytest.raises(psycopg.errors.NotNullViolation), migrated.transaction():
        migrated.execute(
            "INSERT INTO items (source_name, fetch_id, source_item_id, raw_xml, "
            "title, url_raw, canonical_url, content_hash, title_simhash, spec_hash) "
            "VALUES ('s', gen_random_uuid(), 'x', '', 't', 'u', 'u', '', 1, 'h')"
        )
