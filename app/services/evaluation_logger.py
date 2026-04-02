"""
Evaluation logging service for the Sales Operating System.

Provides structured event logging for recommendations, agent executions,
and approval decisions to support ML training dataset export and analysis.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def log_event(
    db: Database,
    event_type: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    outcome: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Insert a single event into evaluation_logs.

    Returns the new log entry id.
    """
    log_id = _uid()
    db.insert(
        "evaluation_logs",
        {
            "id": log_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload_json": json.dumps(payload) if payload is not None else None,
            "outcome": outcome,
            "metadata_json": json.dumps(metadata) if metadata is not None else None,
            "created_at": _now(),
        },
    )
    return log_id


def get_evaluation_log(db: Database, entity_id: str) -> List[Dict[str, Any]]:
    """Return all evaluation_logs rows for a given entity_id."""
    rows = db.query(
        "SELECT * FROM evaluation_logs WHERE entity_id = ? ORDER BY created_at DESC",
        [entity_id],
    )
    return rows


def get_event_summary(
    db: Database,
    event_type: Optional[str] = None,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return aggregate counts grouped by event_type and outcome.

    Optional filters:
      - event_type: restrict to a single event type
      - since: ISO datetime string; only include rows with created_at >= since
    """
    clauses: List[str] = []
    params: List[Any] = []

    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT event_type, outcome, COUNT(*) AS count "
        f"FROM evaluation_logs {where} "
        f"GROUP BY event_type, outcome "
        f"ORDER BY event_type, outcome"
    )
    return db.query(sql, params)
