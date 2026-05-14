"""
Alembic environment for Sales Operating System.

Supports both async (asyncpg / aiosqlite) and the legacy sync path that
Alembic's autogenerate uses internally.  The DATABASE_URL env var drives
everything; falls back to the local SQLite dev database if unset.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ---------------------------------------------------------------------------
# Alembic Config object (gives access to alembic.ini values)
# ---------------------------------------------------------------------------
config = context.config

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import ALL models so autogenerate can see their metadata.
# Add additional model imports here as new ORM models are introduced.
# ---------------------------------------------------------------------------
# The current codebase uses a raw-SQL Database class; the Base below is the
# SQLAlchemy ORM base from app/database.py.  Any future ORM models must
# subclass Base so Alembic picks them up automatically.
from app.database import Base  # noqa: F401 — ensures Base.metadata is populated

# If you later add ORM model files, import them here, e.g.:
# from app.models.orm import ProductCatalogORM, ClientORM  # noqa: F401

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Resolve DATABASE_URL
# ---------------------------------------------------------------------------
_raw_url: str | None = os.environ.get("DATABASE_URL")

if _raw_url:
    # Normalize scheme for SQLAlchemy async engine
    if _raw_url.startswith("postgres://"):
        _raw_url = _raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _raw_url.startswith("postgresql://") and "+asyncpg" not in _raw_url:
        _raw_url = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    _db_url = _raw_url
else:
    # Local dev fallback
    _db_url = "sqlite+aiosqlite:///./sales_os.db"

config.set_main_option("sqlalchemy.url", _db_url)


# ---------------------------------------------------------------------------
# Offline migrations (generate SQL without a live DB connection)
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Render TIMESTAMP WITH TIME ZONE for Postgres
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations (connect to DB and apply changes)
# ---------------------------------------------------------------------------

def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine (asyncpg or aiosqlite)."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
