"""Guards every migration that backfills from a live table must apply.

These live in the `archive` package rather than beside the migrations because
the version files are named `0004_...` and so on — not importable module names,
since alembic loads them by path. Sharing them here keeps one definition
instead of a copy per migration that drifts.

The rules and the reasoning behind each are in
docs/data-model.md#writing-a-migration-that-backfills.
"""

from __future__ import annotations

import logging
import time

from alembic import op

log = logging.getLogger("alembic.runtime.migration")

# The fetch job's advisory lock (newsbot/runner.py FETCH_LOCK_NAME). Held
# across a backfill so rows written mid-migration cannot be missed.
FETCH_LOCK_NAME = "newsbot:fetch"
LOCK_ATTEMPTS = 60
LOCK_WAIT_SECONDS = 2

# Clocks differ: first_seen_utc is minted client-side from the UUIDv7 timestamp
# (migration 0004) while a snapshot boundary comes from the database. Catch-up
# passes are idempotent, so err wide — over-scanning costs a little I/O,
# under-scanning costs correctness.
CATCH_UP_MARGIN = "5 minutes"


def lift_timeouts() -> None:
    """Remove the role-level timeouts for this transaction only.

    Migration 0003 set `statement_timeout = '30s'` and
    `idle_in_transaction_session_timeout = '60s'` on the role, and migrations
    run as that role. Every real backfill exceeds 30 s at production size, so
    without this they are cancelled mid-statement — on production only, never
    on a small development database, which is the worst way to find out.

    SET LOCAL reverts at commit, so normal operation keeps the bound.
    """
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL idle_in_transaction_session_timeout = 0")


def set_lock_timeout(value: str = "5s") -> None:
    """Fail fast rather than queueing ahead of every subsequent query.

    Call before any ACCESS EXCLUSIVE operation: without it, a migration that
    blocks behind a long reader also blocks everything that arrives after it,
    turning a slow migration into an outage.
    """
    op.execute(f"SET LOCAL lock_timeout = '{value}'")


def take_fetch_lock(attempts: int = LOCK_ATTEMPTS, wait: float = LOCK_WAIT_SECONDS) -> None:
    """Pause ingest for the duration of the backfill.

    `pg_try_advisory_xact_lock`, never the session-scoped `pg_advisory_lock`:
    the session form does not release at commit, only when the session ends, so
    a migration that raised — or a runner holding its connection open — would
    leave ingest dead with no sign of why. The xact form releases on commit
    *and* rollback, and still conflicts correctly with the fetch job's
    session-scoped lock because both share one advisory-lock space.

    Acquiring is itself a statement subject to `statement_timeout`, so call
    `lift_timeouts()` first.
    """
    bind = op.get_bind()
    for attempt in range(1, attempts + 1):
        locked = bind.exec_driver_sql(
            "SELECT pg_try_advisory_xact_lock(hashtext(%(name)s))",
            {"name": FETCH_LOCK_NAME},
        ).scalar()
        if locked:
            if attempt > 1:
                log.info("acquired %s after %.0fs", FETCH_LOCK_NAME, (attempt - 1) * wait)
            return
        time.sleep(wait)

    # Never proceed without it. A "best effort, continue anyway" fallback would
    # reintroduce exactly the lost-write bug the lock exists to prevent; a
    # failed deploy is visible and retryable, a silent hole is neither.
    raise RuntimeError(
        f"could not acquire {FETCH_LOCK_NAME} within {attempts * wait:.0f}s — a fetch "
        "run is still holding it. The schema is untouched; retry once it finishes."
    )
