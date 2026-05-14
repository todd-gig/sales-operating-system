"""
SQLAlchemy async engine + session factory for the Sales Operating System.

Production:  reads DATABASE_URL from environment — expects a Cloud SQL
             postgresql+asyncpg URL (Unix-socket form for Cloud Run).
Local dev:   falls back to SQLite via aiosqlite when DATABASE_URL is unset.

Connection-pool settings are tuned for Cloud Run (small, short-lived containers):
  pool_size=5, max_overflow=2, pool_recycle=300s
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# ── URL resolution ────────────────────────────────────────────────────────────
# Priority:
#   1. DATABASE_URL env (explicit full URL, normalized to postgresql+asyncpg)
#   2. DB_HOST + DB_NAME + DB_USER + DB_PASSWORD env (Cloud Run + Secret Manager
#      pattern from cloudbuild-cloud-sql.yaml — DB_HOST is /cloudsql/<instance>
#      Unix-socket path injected by Cloud SQL Auth Proxy)
#   3. SQLite fallback for local dev

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

if not _DATABASE_URL:
    _DB_HOST = os.environ.get("DB_HOST")
    _DB_NAME = os.environ.get("DB_NAME")
    _DB_USER = os.environ.get("DB_USER")
    _DB_PASSWORD = os.environ.get("DB_PASSWORD")
    if all([_DB_HOST, _DB_NAME, _DB_USER, _DB_PASSWORD]):
        # Cloud SQL Auth Proxy sets DB_HOST=/cloudsql/<connection-name>.
        # The asyncpg driver accepts host=<unix-socket-dir> via query param.
        scheme = os.environ.get("DATABASE_URL_SCHEME", "postgresql+asyncpg")
        from urllib.parse import quote_plus
        _DATABASE_URL = (
            f"{scheme}://"
            f"{quote_plus(_DB_USER)}:{quote_plus(_DB_PASSWORD)}"
            f"@/{quote_plus(_DB_NAME)}"
            f"?host={_DB_HOST}"
        )

if _DATABASE_URL:
    # Normalize: plain postgres:// → postgresql+asyncpg://
    if _DATABASE_URL.startswith("postgres://"):
        _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in _DATABASE_URL:
        _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    _IS_SQLITE = False
else:
    _DATABASE_URL = "sqlite+aiosqlite:///./sales_os.db"
    _IS_SQLITE = True

# ── Engine ────────────────────────────────────────────────────────────────────

if _IS_SQLITE:
    # SQLite has no real connection pool; use StaticPool for local dev.
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        _DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=bool(os.environ.get("SQL_ECHO", "")),
    )
else:
    engine = create_async_engine(
        _DATABASE_URL,
        pool_size=5,
        max_overflow=2,
        pool_recycle=300,          # recycle connections every 5 minutes
        pool_pre_ping=True,        # validate connections before use
        echo=bool(os.environ.get("SQL_ECHO", "")),
    )

# ── Session factory ───────────────────────────────────────────────────────────

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ── Declarative base (used by ORM models if/when added) ───────────────────────

class Base(DeclarativeBase):
    pass


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession; for use as a FastAPI Depends() injection."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Convenience: create all tables (used in tests / local bootstrap) ─────────

async def create_all_tables() -> None:
    """Create all tables declared on Base. Not used in production (Alembic handles that)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Expose driver info for health checks ─────────────────────────────────────

DATABASE_BACKEND: str = "sqlite" if _IS_SQLITE else "postgres"
