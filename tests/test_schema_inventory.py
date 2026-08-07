"""Every table must be accounted for, in the fixtures and in the docs.

On a disk that cannot grow, a table nobody has thought about is how you run
out of space. Two inventories have to stay in step with the schema, and both
drift silently when a migration adds a table:

* ``tests/conftest.py``'s ALL_TABLES — a missing entry leaks state between
  tests and surfaces as an unrelated failure somewhere else;
* ``docs/data-model.md``'s retention table — a missing row means a table with
  no stated policy, which is the thing the storage budget cannot survive.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import ALL_TABLES

DOC = Path("docs/data-model.md")

# Alembic's own bookkeeping; not part of the schema this project designs.
IGNORED = {"alembic_version"}


def _live_tables(db) -> set[str]:
    rows = db.execute(
        """
        SELECT c.relname AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          -- Partition children are storage for their parent, not tables in
          -- their own right: they are created and dropped by the partition
          -- maintenance job, and neither inventory should list them.
          AND NOT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = c.oid)
        """
    ).fetchall()
    return {r["name"] for r in rows} - IGNORED


def test_truncate_list_covers_every_table(db):
    live = _live_tables(db)
    listed = set(ALL_TABLES)

    assert not (live - listed), (
        f"tables missing from tests/conftest.py ALL_TABLES: {sorted(live - listed)} — "
        "state will leak between tests"
    )
    assert not (listed - live), (
        f"ALL_TABLES names tables that no longer exist: {sorted(listed - live)}"
    )


def test_every_table_has_a_retention_policy_in_the_docs(db):
    """A table with no row in the retention table is a bug, including the ones
    whose answer is 'never'."""
    text = DOC.read_text()
    section = text.split("## Retention", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"`([a-z_]+)`", section))

    undocumented = sorted(t for t in _live_tables(db) if t not in documented)
    assert not undocumented, (
        f"{undocumented} have no line in {DOC}'s retention table. Every table needs "
        "one, even if the policy is 'never deleted' — see the section's preamble."
    )
