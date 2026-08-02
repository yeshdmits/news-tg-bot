"""Alembic environment: runs the plain-SQL migrations against DATABASE_URL."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

# Plain SQL migrations only — no model metadata, no autogenerate.
target_metadata = None


def database_url() -> str:
    """The migration URL from DATABASE_URL; raises RuntimeError when unset."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # The app uses psycopg directly with a plain postgresql:// URL; SQLAlchemy
    # needs the driver spelled out to pick psycopg v3.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    """Emit migration SQL without a database connection (alembic --sql)."""
    context.configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations over a live connection using a throwaway engine."""
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
