"""
Agent runtime for the Sales Operating System.

State machine
─────────────
    draft ──► ready ──► running ──► awaiting_approval
                                         │
                          ┌──────────────┼──────────────┐
                          ▼              ▼              ▼
                      completed        failed        canceled

Transitions
───────────
  deploy()   → creates a deployment record with status='draft', then advances to 'ready'
  execute()  → 'ready' → 'running' → 'awaiting_approval' (if approval needed) or 'completed'
  approve()  → 'awaiting_approval' → 'completed'
  cancel()   → any non-terminal → 'canceled'
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.models.database import Database
from app.services.evaluation_logger import log_event


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TERMINAL_STATES = {"completed", "failed", "canceled"}
VALID_TRANSITIONS: Dict[str, List[str]] = {
    "draft": ["ready", "canceled"],
    "ready": ["running", "canceled"],
    "running": ["awaiting_approval", "completed", "failed", "canceled"],
    "awaiting_approval": ["completed", "failed", "canceled"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses mirroring DB rows
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentTemplateRecord:
    id: str
    name: str
    purpose: Optional[str] = None
    system_prompt: Optional[str] = None
    tool_policy_json: Optional[str] = None
    output_schema_json: Optional[str] = None
    approval_mode: Optional[str] = None   # "none" | "always" | "on_action"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AgentTemplateRecord":
        return cls(**{k: row.get(k) for k in cls.__dataclass_fields__})


@dataclass
class AgentDeploymentRecord:
    id: str
    agent_template_id: Optional[str] = None
    name: Optional[str] = None
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    status: str = "draft"
    config_json: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AgentDeploymentRecord":
        return cls(**{k: row.get(k) for k in cls.__dataclass_fields__})

    @property
    def config(self) -> Dict[str, Any]:
        if self.config_json:
            try:
                return json.loads(self.config_json)
            except json.JSONDecodeError:
                pass
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Built-in agent handler functions
# ─────────────────────────────────────────────────────────────────────────────

def proposal_agent(deployment: AgentDeploymentRecord, db: Database) -> Dict[str, Any]:
    """Generate a proposal draft for the scoped opportunity."""
    cfg = deployment.config
    opportunity_id = cfg.get("opportunity_id") or deployment.scope_id
    opp = db.get("opportunities", opportunity_id) if opportunity_id else None
    client = db.get("clients", opp["client_id"]) if opp and opp.get("client_id") else None

    return {
        "agent": "proposal_agent",
        "opportunity_id": opportunity_id,
        "client_name": client["name"] if client else "Unknown",
        "stage": opp["stage"] if opp else None,
        "proposal_draft": (
            f"Proposal for {client['name'] if client else 'client'}: "
            f"addressing need '{opp.get('detected_need_summary', 'TBD')}'"
        ),
        "status": "draft_ready",
    }


def discovery_agent(deployment: AgentDeploymentRecord, db: Database) -> Dict[str, Any]:
    """Extract need states from opportunity notes / summary."""
    cfg = deployment.config
    opportunity_id = cfg.get("opportunity_id") or deployment.scope_id
    opp = db.get("opportunities", opportunity_id) if opportunity_id else None

    detected: List[str] = []
    if opp and opp.get("detected_need_summary"):
        all_ns = db.list_all("need_states")
        summary_lower = opp["detected_need_summary"].lower()
        for ns in all_ns:
            pn = (ns.get("problem_name") or "").lower()
            if pn and pn in summary_lower:
                detected.append(ns["id"])

    return {
        "agent": "discovery_agent",
        "opportunity_id": opportunity_id,
        "detected_need_state_ids": detected,
        "signal_count": len(detected),
    }


def recommendation_agent(deployment: AgentDeploymentRecord, db: Database) -> Dict[str, Any]:
    """Run the recommendation engine and return top recommendations."""
    from app.services.recommendation_engine import generate_recommendations

    cfg = deployment.config
    opportunity_id = cfg.get("opportunity_id") or deployment.scope_id

    if not opportunity_id:
        return {"agent": "recommendation_agent", "error": "No opportunity_id provided"}

    recs = generate_recommendations(opportunity_id, db)
    return {
        "agent": "recommendation_agent",
        "opportunity_id": opportunity_id,
        "recommendation_count": len(recs),
        "top_recommendations": [
            {
                "product_id": r.product_id,
                "product_name": r.product_name,
                "type": r.recommendation_type,
                "confidence": r.confidence_score,
                "rationale": r.rationale,
            }
            for r in recs[:10]
        ],
    }


def followup_agent(deployment: AgentDeploymentRecord, db: Database) -> Dict[str, Any]:
    """Draft a follow-up message for stale opportunities."""
    cfg = deployment.config
    opportunity_id = cfg.get("opportunity_id") or deployment.scope_id
    opp = db.get("opportunities", opportunity_id) if opportunity_id else None
    client = db.get("clients", opp["client_id"]) if opp and opp.get("client_id") else None

    return {
        "agent": "followup_agent",
        "opportunity_id": opportunity_id,
        "follow_up_draft": (
            f"Hi {client['name'] if client else 'there'}, "
            f"following up on our discussion about '{opp.get('title', 'your needs')}'. "
            f"Happy to share more details."
        ),
        "channel": cfg.get("channel", "email"),
    }


def sync_agent(deployment: AgentDeploymentRecord, db: Database) -> Dict[str, Any]:
    """Create a google_sync_job record for the configured target."""
    cfg = deployment.config
    job_id = _uid()
    now = _now()
    db.insert(
        "google_sync_jobs",
        {
            "id": job_id,
            "job_type": cfg.get("job_type", "export"),
            "target_google_id": cfg.get("target_google_id"),
            "status": "queued",
            "payload_json": json.dumps(cfg),
            "result_json": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "agent": "sync_agent",
        "google_sync_job_id": job_id,
        "status": "queued",
    }


# Registry mapping template name → handler
HANDLER_REGISTRY: Dict[str, Callable[[AgentDeploymentRecord, Database], Dict[str, Any]]] = {
    "proposal_agent": proposal_agent,
    "discovery_agent": discovery_agent,
    "recommendation_agent": recommendation_agent,
    "followup_agent": followup_agent,
    "sync_agent": sync_agent,
}


# ─────────────────────────────────────────────────────────────────────────────
# AgentRuntime
# ─────────────────────────────────────────────────────────────────────────────

class AgentRuntime:
    """
    Manages the full lifecycle of agent deployments.

    All state is persisted to the Database so the runtime is stateless
    between requests.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── State machine helper ──────────────────────────────────────────────────

    def _transition(self, deployment_id: str, new_status: str) -> AgentDeploymentRecord:
        row = self.db.get("agent_deployments", deployment_id)
        if not row:
            raise ValueError(f"Deployment {deployment_id!r} not found")
        dep = AgentDeploymentRecord.from_row(row)
        allowed = VALID_TRANSITIONS.get(dep.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition deployment from {dep.status!r} to {new_status!r}. "
                f"Allowed: {allowed}"
            )
        self.db.update("agent_deployments", deployment_id, {"status": new_status})
        dep.status = new_status
        return dep

    # ── Public API ────────────────────────────────────────────────────────────

    def deploy(
        self,
        template_id: str,
        scope_type: str,
        scope_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a deployment from a template and advance it to 'ready'.

        Returns the new deployment_id.
        """
        template_row = self.db.get("agent_templates", template_id)
        if not template_row:
            raise ValueError(f"Agent template {template_id!r} not found")

        now = _now()
        dep_id = _uid()
        self.db.insert(
            "agent_deployments",
            {
                "id": dep_id,
                "agent_template_id": template_id,
                "name": template_row.get("name"),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "status": "draft",
                "config_json": json.dumps(config or {}),
                "created_at": now,
                "updated_at": now,
            },
        )
        # Advance draft → ready immediately
        self._transition(dep_id, "ready")
        return dep_id

    def execute(self, deployment_id: str) -> Dict[str, Any]:
        """
        Advance a deployment from 'ready' to 'running', run the handler,
        then advance to 'awaiting_approval' or 'completed'.

        Returns the handler output dict.
        """
        dep = self._transition(deployment_id, "running")

        template_row = self.db.get("agent_templates", dep.agent_template_id) if dep.agent_template_id else None
        handler_name = (template_row or {}).get("name", "")
        handler = HANDLER_REGISTRY.get(handler_name)

        result: Dict[str, Any] = {}
        error: Optional[str] = None

        try:
            if handler:
                result = handler(dep, self.db)
            else:
                result = {
                    "agent": handler_name or "unknown",
                    "message": "No handler registered for this agent template.",
                    "config": dep.config,
                }
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            self.db.update("agent_deployments", deployment_id, {"status": "failed"})
            return {"status": "failed", "error": error}

        # Determine next state
        approval_mode = (template_row or {}).get("approval_mode", "none")
        if approval_mode in ("always", "on_action"):
            next_status = "awaiting_approval"
        else:
            next_status = "completed"

        self._transition(deployment_id, next_status)

        # Persist result in config_json (merge)
        existing_cfg = dep.config
        existing_cfg["_last_result"] = result
        self.db.update(
            "agent_deployments",
            deployment_id,
            {"config_json": json.dumps(existing_cfg)},
        )

        return {"status": next_status, "result": result}

    def approve(self, deployment_id: str) -> Dict[str, Any]:
        """Advance 'awaiting_approval' → 'completed'."""
        row = self.db.get("agent_deployments", deployment_id)
        if not row:
            raise ValueError(f"Deployment {deployment_id!r} not found")
        if row["status"] != "awaiting_approval":
            raise ValueError(
                f"Cannot approve deployment {deployment_id!r}: "
                f"expected status 'awaiting_approval', got {row['status']!r}"
            )
        self._transition(deployment_id, "completed")
        log_event(
            self.db,
            "approval_granted",
            "deployment",
            deployment_id,
            {"deployment_id": deployment_id},
            "success",
        )
        return {"deployment_id": deployment_id, "status": "completed"}

    def reject(self, deployment_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Reject 'awaiting_approval' → 'canceled', storing reason in config_json."""
        row = self.db.get("agent_deployments", deployment_id)
        if not row:
            raise ValueError(f"Deployment {deployment_id!r} not found")
        if row["status"] != "awaiting_approval":
            raise ValueError(
                f"Cannot reject deployment {deployment_id!r}: "
                f"expected status 'awaiting_approval', got {row['status']!r}"
            )
        # Store rejection reason in config_json
        try:
            cfg = json.loads(row.get("config_json") or "{}")
        except json.JSONDecodeError:
            cfg = {}
        cfg["_rejection_reason"] = reason or ""
        self.db.update(
            "agent_deployments",
            deployment_id,
            {"status": "canceled", "config_json": json.dumps(cfg)},
        )
        log_event(
            self.db,
            "approval_rejected",
            "deployment",
            deployment_id,
            {"deployment_id": deployment_id, "reason": reason},
            "failure",
        )
        return {"deployment_id": deployment_id, "status": "canceled", "reason": reason}

    def cancel(self, deployment_id: str) -> Dict[str, Any]:
        """Cancel a non-terminal deployment."""
        row = self.db.get("agent_deployments", deployment_id)
        if not row:
            raise ValueError(f"Deployment {deployment_id!r} not found")
        if row["status"] in TERMINAL_STATES:
            raise ValueError(f"Deployment is already in terminal state {row['status']!r}")
        self.db.update("agent_deployments", deployment_id, {"status": "canceled"})
        return {"deployment_id": deployment_id, "status": "canceled"}

    def get_status(self, deployment_id: str) -> Dict[str, Any]:
        """Return the current deployment row as a dict."""
        row = self.db.get("agent_deployments", deployment_id)
        if not row:
            raise ValueError(f"Deployment {deployment_id!r} not found")
        return dict(row)

    def list_deployments(
        self,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {}
        if scope_type:
            filters["scope_type"] = scope_type
        if scope_id:
            filters["scope_id"] = scope_id
        if status:
            filters["status"] = status
        return self.db.list_all("agent_deployments", filters or None)

    # ── Template management ───────────────────────────────────────────────────

    def register_template(
        self,
        name: str,
        purpose: Optional[str] = None,
        system_prompt: Optional[str] = None,
        tool_policy: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        approval_mode: str = "none",
    ) -> str:
        """Insert a new agent template and return its id."""
        now = _now()
        tid = _uid()
        self.db.insert(
            "agent_templates",
            {
                "id": tid,
                "name": name,
                "purpose": purpose,
                "system_prompt": system_prompt,
                "tool_policy_json": json.dumps(tool_policy or {}),
                "output_schema_json": json.dumps(output_schema or {}),
                "approval_mode": approval_mode,
                "created_at": now,
                "updated_at": now,
            },
        )
        return tid

    def seed_builtin_templates(self) -> None:
        """
        Ensure the five built-in agent templates exist in the database.
        Safe to call multiple times (no-op if already present).
        """
        builtin_defs = [
            ("proposal_agent", "Generate proposal drafts for opportunities", "none"),
            ("discovery_agent", "Extract need states from opportunity context", "none"),
            ("recommendation_agent", "Run recommendation engine for an opportunity", "none"),
            ("followup_agent", "Draft follow-up messages for stale opportunities", "on_action"),
            ("sync_agent", "Queue a Google Workspace sync job", "always"),
        ]
        for name, purpose, approval_mode in builtin_defs:
            existing = self.db.query(
                "SELECT id FROM agent_templates WHERE name = ?", [name]
            )
            if not existing:
                self.register_template(
                    name=name,
                    purpose=purpose,
                    approval_mode=approval_mode,
                )
