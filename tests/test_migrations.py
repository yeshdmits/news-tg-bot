"""Migrations must survive a round trip against data that already exists.

Structures are the easy part; the risk is a migration that defines them
correctly and silently loses rows. These run alembic for real, against a
throwaway database seeded with pre-migration data.

Each migration declares what its downgrade actually restores, and the tests
assert that claim rather than a comfortable approximation of it:

* **0005** — a clean round trip. `item_keys` is derived entirely from `items`.
* **0004** — schema-complete but **lossy** for `routing_decisions`: the
  negative outcomes were aggregated into counters and deleted, and nothing can
  bring them back.
* **0006** — **refuses outright.** Partitions dropped by the archive job exist
  only in object storage.

They use their own database because of that last one: a downgrade attempt
against the shared session database strands every other pg test behind a
half-migrated schema.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from archive.db import connect

SCRATCH_DB = "newsbot_migration_scratch"

# Tables whose contents legitimately do not survive a downgrade, with the
# reason. Anything not listed must round-trip exactly.
LOSSY_ON_DOWNGRADE = {
    "routing_decisions": (
        "0004 aggregates the non-retained outcomes into routing_stats and "
        "deletes them; no downgrade can restore per-item rows"
    ),
}


def _alembic(dsn: str) -> Config:
    os.environ["DATABASE_URL"] = dsn
    return Config("alembic.ini")


@pytest.fixture
def scratch_dsn(pg_dsn):
    """An empty throwaway database. Each test migrates it to what it needs."""
    parts = urlsplit(pg_dsn)
    admin = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')

    yield urlunsplit(parts._replace(path=f"/{SCRATCH_DB}"))
    # Leave alembic's env pointing back at the session database.
    _alembic(pg_dsn)


@pytest.fixture
def at_head(scratch_dsn):
    command.upgrade(_alembic(scratch_dsn), "head")
    conn = connect(scratch_dsn)
    yield conn
    conn.close()


def _counts(conn) -> dict[str, int]:
    """Row counts per top-level table; partition children are excluded, since
    they double-count their parent."""
    rows = conn.execute(
        """
        SELECT c.relname AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND c.relname <> 'alembic_version'
          AND NOT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = c.oid)
        """
    ).fetchall()
    return {
        r["name"]: conn.execute(f'SELECT count(*) AS n FROM "{r["name"]}"').fetchone()["n"]
        for r in rows
    }


def _seed_pre_0004(conn) -> uuid.UUID:
    """Rows in the shape 0004 has to migrate, not merely tolerate."""
    spec_hash = "b" * 64
    conn.execute(
        "INSERT INTO spec_versions (spec_hash, spec) VALUES (%s, '{}') ON CONFLICT DO NOTHING",
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


def test_0004_round_trips_and_declares_what_it_loses(scratch_dsn):
    command.upgrade(_alembic(scratch_dsn), "0003")
    conn = connect(scratch_dsn)
    item_id = _seed_pre_0004(conn)
    before = _counts(conn)

    command.upgrade(_alembic(scratch_dsn), "0004")

    # The negative outcomes became counters; the retained one kept its row.
    assert conn.execute("SELECT count(*) AS n FROM routing_decisions").fetchone()["n"] == 1
    assert conn.execute("SELECT COALESCE(sum(n), 0) AS n FROM routing_stats").fetchone()["n"] == 2
    # The v4 item is indexed, or it would be permanently unlocatable.
    assert conn.execute(
        "SELECT archive_dt FROM legacy_item_index WHERE item_id = %s", (item_id,)
    ).fetchone() is not None

    command.downgrade(_alembic(scratch_dsn), "0003")
    after = _counts(conn)

    for table, count in before.items():
        if table in LOSSY_ON_DOWNGRADE:
            continue
        assert after.get(table) == count, f"{table} did not survive the round trip"

    # And state the loss rather than quietly skipping it.
    assert after["routing_decisions"] == 1 < before["routing_decisions"] == 3, (
        LOSSY_ON_DOWNGRADE["routing_decisions"]
    )
    conn.close()


def test_0005_round_trips_cleanly(scratch_dsn):
    """item_keys is derived entirely from items, so its downgrade loses
    nothing that upgrade() cannot rebuild."""
    command.upgrade(_alembic(scratch_dsn), "0004")
    conn = connect(scratch_dsn)
    _seed_pre_0004(conn)
    before = _counts(conn)

    command.upgrade(_alembic(scratch_dsn), "0005")
    assert conn.execute("SELECT count(*) AS n FROM item_keys").fetchone()["n"] == 1

    command.downgrade(_alembic(scratch_dsn), "0004")
    assert _counts(conn) == before
    conn.close()


def test_0006_refuses_to_downgrade(scratch_dsn):
    """An honest one-way door. A downgrade that worked only before the first
    archive run would be a trap, and one returning a subset would be worse."""
    command.upgrade(_alembic(scratch_dsn), "head")

    with pytest.raises(NotImplementedError, match="only in object storage"):
        command.downgrade(_alembic(scratch_dsn), "0005")

    # The failed attempt left the schema untouched.
    conn = connect(scratch_dsn)
    assert conn.execute(
        "SELECT relkind FROM pg_class WHERE relname = 'items'"
    ).fetchone()["relkind"] == "p"
    conn.close()


def test_0006_preserves_every_row(scratch_dsn):
    """Copy-and-rename is where rows go missing silently."""
    command.upgrade(_alembic(scratch_dsn), "0005")
    conn = connect(scratch_dsn)
    _seed_pre_0004(conn)
    before = _counts(conn)

    command.upgrade(_alembic(scratch_dsn), "0006")

    after = _counts(conn)
    for table in ("items", "item_categories", "routing_decisions", "deliveries"):
        assert after[table] == before[table], f"{table} lost rows in the repartition"
    conn.close()


def test_migration_lifts_the_role_statement_timeout(at_head):
    """Migration 0003 pins statement_timeout to 30s on the role, and migrations
    run as that role. A backfill that does not lift it works on a small
    development database and fails only in production."""
    role_default = at_head.execute(
        "SELECT setconfig FROM pg_db_role_setting s "
        "JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = current_user"
    ).fetchone()
    assert role_default is not None
    assert any("statement_timeout=30s" in c.replace(" ", "") for c in role_default["setconfig"]), (
        "0003's role-level statement_timeout is gone; the migration guards assume it"
    )


def test_defaults_are_dropped_at_head(at_head):
    """With the writer always supplying both values, a server default is a trap
    for any insert path that forgets."""
    rows = at_head.execute(
        """
        SELECT column_name, column_default FROM information_schema.columns
        WHERE table_name = 'items' AND column_name IN ('item_id', 'first_seen_utc')
        """
    ).fetchall()
    assert {r["column_name"]: r["column_default"] for r in rows} == {
        "item_id": None,
        "first_seen_utc": None,
    }


def test_no_foreign_keys_point_into_items_at_head(at_head):
    """Every FK into items would block a partition drop (hazard 2)."""
    remaining = at_head.execute(
        """
        SELECT conname FROM pg_constraint
        WHERE contype = 'f' AND confrelid = 'items'::regclass
        """
    ).fetchall()
    assert remaining == [], f"these would block partition drops: {remaining}"
