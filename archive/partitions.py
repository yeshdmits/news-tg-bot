"""Daily partition maintenance for items and its dependants.

**Inserts fail hard once they run past the last partition.** There is no
"default partition" here on purpose: routing a row with no home into a catch-all
would hide the shortfall until the catch-all was itself undroppable. A missing
partition must be loud, and it must be seen before it happens — hence the
lookahead and the alertable shortfall check.

pg_partman would do this, but it is not in the production server's
azure.extensions allow-list (nor is pg_cron), and enabling it is an
infrastructure change. See docs/infrastructure-runbook.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import psycopg

log = logging.getLogger(__name__)

# Tables partitioned on the same daily boundary, so a day drops as a unit.
# Order matters for dropping: dependants first, then items.
PARTITIONED_TABLES = ("item_categories", "routing_decisions", "deliveries", "items")

# How far ahead partitions are pre-created. The brief's floor is 7 days; 14
# gives a week of slack on top, so a fortnight of failed maintenance is needed
# before ingest stops. Anything below MINIMUM_LOOKAHEAD_DAYS is an alert.
PARTITION_LOOKAHEAD_DAYS = 14
MINIMUM_LOOKAHEAD_DAYS = 7


def _today() -> date:
    """Today in **UTC**, not local time.

    Partitions are keyed on timestamptz values, so a host in a non-UTC zone
    would otherwise provision the wrong day near midnight — creating a gap
    that ingest falls into, on exactly the boundary where it is least obvious.
    """
    return datetime.now(UTC).date()


def partition_name(table: str, day: date) -> str:
    return f"{table}_p{day:%Y%m%d}"


def partition_ddl(
    lo: date | None, hi: date | None, *, lookahead: int, parent_suffix: str = ""
) -> list[str]:
    """CREATE TABLE ... PARTITION OF statements covering [lo, hi] plus lookahead.

    ``parent_suffix`` names the parent during migration 0006, where partitions
    are attached to ``items_partitioned`` before that table is renamed to
    ``items``. Child names stay canonical either way: Postgres keeps children
    attached across a parent rename, and renaming several thousand partitions
    afterwards would be pointless work.

    Idempotent (IF NOT EXISTS), so re-running after a partial failure is safe.
    """
    today = _today()
    start = min(lo, today) if lo else today
    end = max(hi, today) if hi else today
    statements = []
    day = start
    while day <= end + timedelta(days=lookahead):
        for table in PARTITIONED_TABLES:
            statements.append(
                f"CREATE TABLE IF NOT EXISTS {partition_name(table, day)} "
                f"PARTITION OF {table}{parent_suffix} "
                f"FOR VALUES FROM ('{day}') TO ('{day + timedelta(days=1)}')"
            )
        day += timedelta(days=1)
    return statements


def ensure_partitions(
    conn: psycopg.Connection, *, lookahead: int = PARTITION_LOOKAHEAD_DAYS, today: date | None = None
) -> int:
    """Create any missing partitions out to the lookahead; returns how many.

    Cheap enough to run every execution: it is a catalogue lookup plus, on most
    days, nothing.
    """
    today = today or _today()
    created = 0
    for day in (today + timedelta(days=n) for n in range(lookahead + 1)):
        for table in PARTITIONED_TABLES:
            name = partition_name(table, day)
            exists = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL AS present", (name,)
            ).fetchone()["present"]
            if exists:
                continue
            # Interpolated, not bound: PARTITION OF ... FOR VALUES is DDL and
            # takes no parameters. The values are dates this function computed,
            # never caller input.
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
                f"FOR VALUES FROM ('{day}') TO ('{day + timedelta(days=1)}')"
            )
            created += 1
    if created:
        log.info("created %d partitions out to +%dd", created, lookahead)
    return created


def days_of_headroom(conn: psycopg.Connection, *, today: date | None = None) -> int:
    """Consecutive days from today for which an items partition exists.

    This is the number that matters operationally: ingest stops the moment it
    reaches a day with no partition, so this is how long the system survives
    with maintenance broken.
    """
    today = today or _today()
    headroom = 0
    while True:
        name = partition_name("items", today + timedelta(days=headroom))
        present = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS present", (name,)
        ).fetchone()["present"]
        if not present:
            return headroom
        headroom += 1


def oldest_partition_day(conn: psycopg.Connection) -> date | None:
    """The earliest day with an items partition, or None if there are none."""
    row = conn.execute(
        """
        SELECT min(substring(relname from 'items_p([0-9]{8})$')) AS day
        FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid
        WHERE relname ~ '^items_p[0-9]{8}$'
        """
    ).fetchone()
    if not row or not row["day"]:
        return None
    raw = row["day"]
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


def drop_day(conn: psycopg.Connection, day: date) -> list[str]:
    """Drop one day across every partitioned table; returns what was dropped.

    Dependants first, then items — the reverse of what a foreign key would
    demand, except there are no foreign keys between them any more precisely so
    that this can be a plain sequence of DROP TABLEs rather than a cascade.
    """
    dropped = []
    for table in PARTITIONED_TABLES:
        name = partition_name(table, day)
        present = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS present", (name,)
        ).fetchone()["present"]
        if present:
            conn.execute(f"DROP TABLE {name}")
            dropped.append(name)
    return dropped
