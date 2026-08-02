"""Postgres connection handling.

Synchronous psycopg: item volume is tiny, and blocking a few milliseconds
inside the scheduler loop is a fair trade for transaction code that cannot
interleave. Connections are autocommit; multi-statement units of work run
inside ``transaction(conn)`` blocks.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


def connect(dsn: str) -> psycopg.Connection:
    """Open an autocommit connection with dict rows and a UTC session timezone."""
    return psycopg.connect(
        dsn, autocommit=True, row_factory=dict_row, options="-c timezone=UTC"
    )


@contextmanager
def transaction(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """One atomic unit of work; commits on exit, rolls back on exception."""
    with conn.transaction():
        yield conn
