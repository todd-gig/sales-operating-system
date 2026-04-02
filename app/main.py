"""
Sales Operating System — FastAPI application factory.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.models.database import init_global_db
from app.api.routes import router
from app.agents.runtime import AgentRuntime


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    On startup:
      1. Initialise (or migrate) the SQLite database.
      2. Seed the five built-in agent templates if they don't exist yet.
    """
    db_path = os.environ.get("DATABASE_PATH", "sales_os.db")
    db = init_global_db(db_path)

    runtime = AgentRuntime(db)
    runtime.seed_builtin_templates()

    print(f"[SalesOS] Database initialised at '{db_path}'")
    yield
    db.close()
    print("[SalesOS] Database connection closed")


# ─────────────────────────────────────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Sales Operating System",
        description=(
            "Backend API for the Sales Operating System: product catalog, "
            "opportunity management, rules-based recommendations, and agent runtime."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(router, prefix="/api/v1")

    return app


# Module-level app instance (used by uvicorn / gunicorn)
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=True,
    )
