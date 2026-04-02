"""
SQLite database setup and helper utilities for the Sales Operating System.
"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


DDL_STATEMENTS: List[str] = [
    # ── Product Catalog ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS product_catalog (
        id                   TEXT PRIMARY KEY,
        name                 TEXT NOT NULL,
        type                 TEXT,
        category             TEXT,
        subcategory          TEXT,
        description          TEXT,
        primary_goal         TEXT,
        core_value           TEXT,
        interaction_value    INTEGER DEFAULT 1,
        marketing_influence  INTEGER DEFAULT 1,
        score_multiplier     REAL    DEFAULT 0,
        funnel_stage         TEXT,
        primary_channel      TEXT,
        automation_potential TEXT,
        source_reference     TEXT,
        is_active            INTEGER DEFAULT 1,
        created_at           TEXT,
        updated_at           TEXT
    )
    """,
    # ── Bundles ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS bundles (
        id                TEXT PRIMARY KEY,
        name              TEXT NOT NULL,
        description       TEXT,
        value_proposition TEXT,
        created_at        TEXT,
        updated_at        TEXT
    )
    """,
    # ── Bundle Items ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS bundle_items (
        id             TEXT PRIMARY KEY,
        bundle_id      TEXT NOT NULL REFERENCES bundles(id),
        product_id     TEXT NOT NULL REFERENCES product_catalog(id),
        sequence_order INTEGER DEFAULT 0,
        required       INTEGER DEFAULT 1
    )
    """,
    # ── Need States ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS need_states (
        id              TEXT PRIMARY KEY,
        problem_name    TEXT,
        detected_signal TEXT,
        severity        TEXT,
        description     TEXT
    )
    """,
    # ── Need State → Products ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS need_state_products (
        id                    TEXT PRIMARY KEY,
        need_state_id         TEXT NOT NULL REFERENCES need_states(id),
        product_id            TEXT NOT NULL REFERENCES product_catalog(id),
        priority_order        INTEGER DEFAULT 1,
        recommendation_reason TEXT
    )
    """,
    # ── Upsell Rules ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS upsell_rules (
        id                    TEXT PRIMARY KEY,
        primary_product_id    TEXT REFERENCES product_catalog(id),
        trigger_event         TEXT,
        client_need_state_id  TEXT REFERENCES need_states(id),
        recommended_product_id TEXT REFERENCES product_catalog(id),
        upsell_type           TEXT,
        expected_impact       TEXT,
        dependency_product_id TEXT REFERENCES product_catalog(id)
    )
    """,
    # ── Cross-Sell Rules ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cross_sell_rules (
        id               TEXT PRIMARY KEY,
        product_id       TEXT REFERENCES product_catalog(id),
        paired_product_id TEXT REFERENCES product_catalog(id),
        reason           TEXT,
        bundle_strength  INTEGER DEFAULT 3
    )
    """,
    # ── Clients ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS clients (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        segment    TEXT,
        status     TEXT,
        notes      TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    # ── Opportunities ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS opportunities (
        id                   TEXT PRIMARY KEY,
        client_id            TEXT REFERENCES clients(id),
        title                TEXT,
        stage                TEXT,
        detected_need_summary TEXT,
        owner_user_id        TEXT,
        created_at           TEXT,
        updated_at           TEXT
    )
    """,
    # ── Recommendations ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS recommendations (
        id                  TEXT PRIMARY KEY,
        opportunity_id      TEXT REFERENCES opportunities(id),
        recommendation_type TEXT,
        target_product_id   TEXT REFERENCES product_catalog(id),
        confidence_score    REAL,
        rationale           TEXT,
        status              TEXT,
        created_at          TEXT
    )
    """,
    # ── Agent Templates ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS agent_templates (
        id                TEXT PRIMARY KEY,
        name              TEXT NOT NULL,
        purpose           TEXT,
        system_prompt     TEXT,
        tool_policy_json  TEXT,
        output_schema_json TEXT,
        approval_mode     TEXT,
        created_at        TEXT,
        updated_at        TEXT
    )
    """,
    # ── Agent Deployments ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS agent_deployments (
        id                 TEXT PRIMARY KEY,
        agent_template_id  TEXT REFERENCES agent_templates(id),
        name               TEXT,
        scope_type         TEXT,
        scope_id           TEXT,
        status             TEXT DEFAULT 'draft',
        config_json        TEXT,
        created_at         TEXT,
        updated_at         TEXT
    )
    """,
    # ── Workflow Runs ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id            TEXT PRIMARY KEY,
        workflow_type TEXT,
        status        TEXT,
        input_json    TEXT,
        output_json   TEXT,
        started_at    TEXT,
        finished_at   TEXT,
        error_text    TEXT
    )
    """,
    # ── Google Sync Jobs ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS google_sync_jobs (
        id               TEXT PRIMARY KEY,
        job_type         TEXT,
        target_google_id TEXT,
        status           TEXT,
        payload_json     TEXT,
        result_json      TEXT,
        created_at       TEXT,
        updated_at       TEXT
    )
    """,
    # ── Evaluation Logs ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS evaluation_logs (
        id               TEXT PRIMARY KEY,
        event_type       TEXT NOT NULL,
        entity_type      TEXT,
        entity_id        TEXT,
        payload_json     TEXT,
        outcome          TEXT,
        metadata_json    TEXT,
        created_at       TEXT NOT NULL
    )
    """,
]


class Database:
    """Thin wrapper around a SQLite connection with CRUD helpers."""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def init_db(self, path: Optional[str] = None) -> None:
        """Initialise the database, creating tables if they don't exist."""
        if path:
            self._path = path
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        for stmt in DDL_STATEMENTS:
            self._conn.execute(stmt)
        self._conn.commit()

    def get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialised. Call init_db() first.")
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Context manager ───────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Generic CRUD helpers ──────────────────────────────────────────────────

    def insert(self, table: str, data: Dict[str, Any]) -> str:
        """Insert a row; auto-generates id and timestamps when absent."""
        if "id" not in data or not data["id"]:
            data = {**data, "id": _new_id()}
        now = _now()
        if "created_at" in self._columns(table) and "created_at" not in data:
            data["created_at"] = now
        if "updated_at" in self._columns(table) and "updated_at" not in data:
            data["updated_at"] = now

        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        conn = self.get_connection()
        conn.execute(sql, list(data.values()))
        conn.commit()
        return data["id"]

    def get(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_all(
        self,
        table: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        where = ""
        params: List[Any] = []
        if filters:
            clauses = [f"{k} = ?" for k in filters]
            where = "WHERE " + " AND ".join(clauses)
            params = list(filters.values())
        params += [limit, offset]
        rows = conn.execute(
            f"SELECT * FROM {table} {where} LIMIT ? OFFSET ?", params
        ).fetchall()
        return [dict(r) for r in rows]

    def update(self, table: str, row_id: str, data: Dict[str, Any]) -> bool:
        data = {k: v for k, v in data.items() if k != "id"}
        if "updated_at" in self._columns(table):
            data["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in data)
        sql = f"UPDATE {table} SET {set_clause} WHERE id = ?"
        conn = self.get_connection()
        cur = conn.execute(sql, list(data.values()) + [row_id])
        conn.commit()
        return cur.rowcount > 0

    def delete(self, table: str, row_id: str) -> bool:
        conn = self.get_connection()
        cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        conn.commit()
        return cur.rowcount > 0

    def query(
        self, sql: str, params: Optional[List[Any]] = None
    ) -> List[Dict[str, Any]]:
        """Execute an arbitrary SELECT and return rows as dicts."""
        conn = self.get_connection()
        rows = conn.execute(sql, params or []).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: Optional[List[Any]] = None) -> int:
        """Execute a non-SELECT statement and return rowcount."""
        conn = self.get_connection()
        cur = conn.execute(sql, params or [])
        conn.commit()
        return cur.rowcount

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _columns(self, table: str) -> List[str]:
        conn = self.get_connection()
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r["name"] for r in rows]


# Module-level singleton used by the FastAPI app
_db_instance: Optional[Database] = None


def get_db() -> Database:
    """Return the module-level Database singleton (FastAPI dependency)."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


def init_global_db(path: str = "sales_os.db") -> Database:
    """Initialise (or re-initialise) the module-level singleton."""
    global _db_instance
    _db_instance = Database(path)
    _db_instance.init_db()
    return _db_instance
