"""Shared fixtures.

Postgres-backed tests request the ``db`` fixture and are automatically marked
``pg``. Locally they run against the docker-compose db (host port 5433) and
are skipped when it is unreachable; in CI (service container) an unreachable
database is a hard failure, never a silent skip.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

# The full reference spec used by tests that need a realistic configuration.
# It mirrors spec.example.json's structure but keeps all eight sources so the
# publisher mapping features stay covered. All chat ids are fakes from the
# designated 9999000 block (see scripts/check-neutral.sh).
SPEC_FIXTURE = "tests/fixtures/spec-full.json"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://newsbot:newsbot@localhost:5433/newsbot_test",
)

# Every table the schema owns, truncated between tests. Kept honest by
# test_schema_inventory.py, which fails if a migration adds a table and
# forgets this list — a missing entry leaks state across tests and shows up
# as a baffling failure somewhere unrelated.
ALL_TABLES = (
    "deliveries", "routing_decisions", "routing_stats", "translations",
    "item_keys", "item_categories", "error_occurrences", "alert_outbox", "error_events",
    "operator_actions", "bot_state", "seen_updates", "items",
    "legacy_item_index", "fetches", "channel_state", "source_state",
    "spec_versions",
)


def pytest_collection_modifyitems(items):
    for item in items:
        if "db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.pg)


def _ensure_database_exists() -> None:
    parts = urlsplit(TEST_DATABASE_URL)
    dbname = parts.path.lstrip("/")
    admin_url = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin_url, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{dbname}"')


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    try:
        _ensure_database_exists()
    except psycopg.OperationalError as e:
        if os.environ.get("CI"):
            pytest.fail(f"Postgres unreachable in CI: {e}")
        pytest.skip(f"Postgres unreachable, skipping pg tests: {e}")

    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(Config("alembic.ini"), "head")
    return TEST_DATABASE_URL


@pytest.fixture
def db(pg_dsn):
    from archive.db import connect

    conn = connect(pg_dsn)
    conn.execute(f"TRUNCATE {', '.join(ALL_TABLES)} CASCADE")
    yield conn
    conn.close()
