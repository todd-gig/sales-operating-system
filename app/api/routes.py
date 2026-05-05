"""
FastAPI route definitions for the Sales Operating System API.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.models.database import Database, get_db
from app.models.schemas import (
    Bundle,
    BundleCreate,
    BundleItem,
    BundleItemCreate,
    CatalogImportRequest,
    Client,
    ClientCreate,
    DeployAgentRequest,
    Opportunity,
    OpportunityCreate,
    ProductCatalog,
    ProductCatalogCreate,
    RecommendationResult,
    RunWorkflowRequest,
    WorkflowRun,
)
from app.services.recommendation_engine import generate_recommendations
from app.agents.runtime import AgentRuntime
from app.services.evaluation_logger import log_event, get_event_summary
from app.services.claude_reasoning import (
    explain_recommendations,
    draft_proposal,
    detect_need_states,
    is_available as claude_is_available,
)
from app.services.dataset_export import (
    export_recommendations_jsonl,
    export_decisions_csv,
    export_catalog_json,
)
from app.services.google_service import (
    get_auth_status,
    get_oauth_flow,
    exchange_oauth_code,
    import_sheet_to_db,
    export_recommendations_to_sheet,
    create_proposal_doc,
    create_gmail_draft,
    create_followup_draft,
    GoogleAuthError,
)
from app.services.gigaton_pricing import (
    GigatonPricingClient,
    PricingQuoteRequest,
    CostBreakdown,
    get_gigaton_client,
)

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health", tags=["meta"])
def health_check() -> Dict[str, str]:
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/catalog/import-sheet", tags=["catalog"])
def import_catalog_sheet(
    req: CatalogImportRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Stub for Google Sheets import. In production this would use the Sheets API.
    Records the import attempt as a google_sync_job.
    """
    job_id = _uid()
    now = _now()
    db.insert(
        "google_sync_jobs",
        {
            "id": job_id,
            "job_type": "catalog_import",
            "target_google_id": req.sheet_id,
            "status": "queued",
            "payload_json": json.dumps({"sheet_id": req.sheet_id, "range": req.sheet_range}),
            "result_json": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "message": "Catalog import job queued",
        "google_sync_job_id": job_id,
        "sheet_id": req.sheet_id,
    }


@router.get("/catalog/products", response_model=List[ProductCatalog], tags=["catalog"])
def list_products(
    is_active: Optional[int] = None,
    category: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {}
    if is_active is not None:
        filters["is_active"] = is_active
    if category:
        filters["category"] = category
    return db.list_all("product_catalog", filters or None)


@router.post(
    "/catalog/products",
    response_model=ProductCatalog,
    status_code=status.HTTP_201_CREATED,
    tags=["catalog"],
)
def create_product(
    payload: ProductCatalogCreate,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    data = payload.model_dump()
    db.insert("product_catalog", data)
    row = db.get("product_catalog", data["id"])
    if not row:
        raise HTTPException(status_code=500, detail="Insert failed")
    return row


@router.get("/catalog/products/{product_id}", response_model=ProductCatalog, tags=["catalog"])
def get_product(product_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    row = db.get("product_catalog", product_id)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return row


@router.put("/catalog/products/{product_id}", response_model=ProductCatalog, tags=["catalog"])
def update_product(
    product_id: str,
    payload: ProductCatalogCreate,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    if not db.get("product_catalog", product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    db.update("product_catalog", product_id, payload.model_dump(exclude={"id"}))
    return db.get("product_catalog", product_id)


@router.delete("/catalog/products/{product_id}", tags=["catalog"])
def delete_product(product_id: str, db: Database = Depends(get_db)) -> Dict[str, str]:
    if not db.delete("product_catalog", product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": product_id}


# ─────────────────────────────────────────────────────────────────────────────
# Bundles
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/catalog/bundles", response_model=List[Bundle], tags=["catalog"])
def list_bundles(db: Database = Depends(get_db)) -> List[Dict[str, Any]]:
    return db.list_all("bundles")


@router.post(
    "/catalog/bundles",
    response_model=Bundle,
    status_code=status.HTTP_201_CREATED,
    tags=["catalog"],
)
def create_bundle(
    payload: BundleCreate, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    data = payload.model_dump()
    db.insert("bundles", data)
    return db.get("bundles", data["id"])


@router.get("/catalog/bundles/{bundle_id}", response_model=Bundle, tags=["catalog"])
def get_bundle(bundle_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    row = db.get("bundles", bundle_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return row


@router.post(
    "/catalog/bundles/{bundle_id}/items",
    response_model=BundleItem,
    status_code=status.HTTP_201_CREATED,
    tags=["catalog"],
)
def add_bundle_item(
    bundle_id: str,
    payload: BundleItemCreate,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    if not db.get("bundles", bundle_id):
        raise HTTPException(status_code=404, detail="Bundle not found")
    data = payload.model_dump()
    data["bundle_id"] = bundle_id
    db.insert("bundle_items", data)
    return db.get("bundle_items", data["id"])


# ─────────────────────────────────────────────────────────────────────────────
# Clients
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/clients", response_model=List[Client], tags=["clients"])
def list_clients(
    segment: Optional[str] = None,
    status: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {}
    if segment:
        filters["segment"] = segment
    if status:
        filters["status"] = status
    return db.list_all("clients", filters or None)


@router.post(
    "/clients",
    response_model=Client,
    status_code=status.HTTP_201_CREATED,
    tags=["clients"],
)
def create_client(
    payload: ClientCreate, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    data = payload.model_dump()
    db.insert("clients", data)
    return db.get("clients", data["id"])


@router.get("/clients/{client_id}", response_model=Client, tags=["clients"])
def get_client(client_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    row = db.get("clients", client_id)
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    return row


@router.put("/clients/{client_id}", response_model=Client, tags=["clients"])
def update_client(
    client_id: str,
    payload: ClientCreate,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    if not db.get("clients", client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    db.update("clients", client_id, payload.model_dump(exclude={"id"}))
    return db.get("clients", client_id)


@router.delete("/clients/{client_id}", tags=["clients"])
def delete_client(client_id: str, db: Database = Depends(get_db)) -> Dict[str, str]:
    if not db.delete("clients", client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    return {"deleted": client_id}


# ─────────────────────────────────────────────────────────────────────────────
# Opportunities
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/opportunities", response_model=List[Opportunity], tags=["opportunities"])
def list_opportunities(
    client_id: Optional[str] = None,
    stage: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {}
    if client_id:
        filters["client_id"] = client_id
    if stage:
        filters["stage"] = stage
    return db.list_all("opportunities", filters or None)


@router.post(
    "/opportunities",
    response_model=Opportunity,
    status_code=status.HTTP_201_CREATED,
    tags=["opportunities"],
)
def create_opportunity(
    payload: OpportunityCreate, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    data = payload.model_dump()
    db.insert("opportunities", data)
    return db.get("opportunities", data["id"])


@router.get(
    "/opportunities/{opportunity_id}",
    response_model=Opportunity,
    tags=["opportunities"],
)
def get_opportunity(
    opportunity_id: str, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    row = db.get("opportunities", opportunity_id)
    if not row:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return row


@router.put(
    "/opportunities/{opportunity_id}",
    response_model=Opportunity,
    tags=["opportunities"],
)
def update_opportunity(
    opportunity_id: str,
    payload: OpportunityCreate,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    if not db.get("opportunities", opportunity_id):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    db.update("opportunities", opportunity_id, payload.model_dump(exclude={"id"}))
    return db.get("opportunities", opportunity_id)


@router.delete("/opportunities/{opportunity_id}", tags=["opportunities"])
def delete_opportunity(
    opportunity_id: str, db: Database = Depends(get_db)
) -> Dict[str, str]:
    if not db.delete("opportunities", opportunity_id):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return {"deleted": opportunity_id}


# ─────────────────────────────────────────────────────────────────────────────
# Recommendations
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/opportunities/{opportunity_id}/recommendations",
    response_model=List[RecommendationResult],
    tags=["recommendations"],
)
def get_opportunity_recommendations(
    opportunity_id: str, db: Database = Depends(get_db)
) -> List[RecommendationResult]:
    if not db.get("opportunities", opportunity_id):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return generate_recommendations(opportunity_id, db)


# ─────────────────────────────────────────────────────────────────────────────
# Agent deployments
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/agents/deploy",
    status_code=status.HTTP_201_CREATED,
    tags=["agents"],
)
def deploy_agent(
    payload: DeployAgentRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    runtime = AgentRuntime(db)
    try:
        dep_id = runtime.deploy(
            template_id=payload.template_id,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            config=payload.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deployment_id": dep_id, "status": "ready"}


@router.post("/agents/deployments/{deployment_id}/execute", tags=["agents"])
def execute_deployment(
    deployment_id: str, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    runtime = AgentRuntime(db)
    try:
        result = runtime.execute(deployment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_event(
        db,
        "agent_executed",
        "deployment",
        deployment_id,
        result,
        result.get("status", "unknown"),
    )
    return result


@router.post("/agents/deployments/{deployment_id}/approve", tags=["agents"])
def approve_deployment(
    deployment_id: str, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    runtime = AgentRuntime(db)
    try:
        return runtime.approve(deployment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/agents/deployments/{deployment_id}/cancel", tags=["agents"])
def cancel_deployment(
    deployment_id: str, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    runtime = AgentRuntime(db)
    try:
        return runtime.cancel(deployment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/agents/deployments/{deployment_id}", tags=["agents"])
def get_deployment(
    deployment_id: str, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    runtime = AgentRuntime(db)
    try:
        return runtime.get_status(deployment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/agents/deployments", tags=["agents"])
def list_agent_deployments(
    scope_type: Optional[str] = None,
    scope_id: Optional[str] = None,
    agent_status: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    runtime = AgentRuntime(db)
    return runtime.list_deployments(scope_type, scope_id, agent_status)


# ─────────────────────────────────────────────────────────────────────────────
# Workflow runs
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/workflow/run",
    status_code=status.HTTP_201_CREATED,
    tags=["workflow"],
)
def run_workflow(
    payload: RunWorkflowRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Execute a named workflow.

    Supported workflow_types:
      - generate_recommendations: run the recommendation engine for an opportunity_id
      - deploy_agent: deploy a built-in agent template by name
      - import_catalog: queue a catalog import
    """
    run_id = _uid()
    now = _now()

    db.insert(
        "workflow_runs",
        {
            "id": run_id,
            "workflow_type": payload.workflow_type,
            "status": "running",
            "input_json": json.dumps(payload.input_data or {}),
            "output_json": None,
            "started_at": now,
            "finished_at": None,
            "error_text": None,
        },
    )

    output: Dict[str, Any] = {}
    error: Optional[str] = None

    try:
        inp = payload.input_data or {}

        if payload.workflow_type == "generate_recommendations":
            opportunity_id = inp.get("opportunity_id")
            if not opportunity_id:
                raise ValueError("input_data.opportunity_id is required")
            recs = generate_recommendations(opportunity_id, db)
            output = {
                "opportunity_id": opportunity_id,
                "recommendation_count": len(recs),
                "recommendations": [r.model_dump() for r in recs],
            }

        elif payload.workflow_type == "deploy_agent":
            template_name = inp.get("template_name")
            if not template_name:
                raise ValueError("input_data.template_name is required")
            runtime = AgentRuntime(db)
            runtime.seed_builtin_templates()
            rows = db.query(
                "SELECT id FROM agent_templates WHERE name = ?", [template_name]
            )
            if not rows:
                raise ValueError(f"Template '{template_name}' not found")
            dep_id = runtime.deploy(
                template_id=rows[0]["id"],
                scope_type=inp.get("scope_type", "global"),
                scope_id=inp.get("scope_id"),
                config=inp.get("config", {}),
            )
            output = {"deployment_id": dep_id, "status": "ready"}

        elif payload.workflow_type == "import_catalog":
            sheet_id = inp.get("sheet_id", "")
            job_id = _uid()
            db.insert(
                "google_sync_jobs",
                {
                    "id": job_id,
                    "job_type": "catalog_import",
                    "target_google_id": sheet_id,
                    "status": "queued",
                    "payload_json": json.dumps(inp),
                    "result_json": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            output = {"google_sync_job_id": job_id, "status": "queued"}

        else:
            output = {
                "workflow_type": payload.workflow_type,
                "message": "Workflow type executed (no-op stub)",
                "input": inp,
            }

    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    finished = _now()
    final_status = "failed" if error else "completed"
    db.update(
        "workflow_runs",
        run_id,
        {
            "status": final_status,
            "output_json": json.dumps(output),
            "finished_at": finished,
            "error_text": error,
        },
    )

    return {
        "run_id": run_id,
        "workflow_type": payload.workflow_type,
        "status": final_status,
        "output": output,
        "error": error,
    }


@router.get("/workflow/runs/{run_id}", response_model=WorkflowRun, tags=["workflow"])
def get_workflow_run(run_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    row = db.get("workflow_runs", run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return row


@router.get("/workflow/runs", response_model=List[WorkflowRun], tags=["workflow"])
def list_workflow_runs(
    workflow_type: Optional[str] = None,
    run_status: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {}
    if workflow_type:
        filters["workflow_type"] = workflow_type
    if run_status:
        filters["status"] = run_status
    return db.list_all("workflow_runs", filters or None)


# ─────────────────────────────────────────────────────────────────────────────
# Execution History (Sprint 4)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/execution/history", tags=["execution"])
def list_execution_history(
    limit: int = 50,
    opportunity_id: Optional[str] = None,
    status: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    """
    Return last N agent deployment executions with their results.

    Joins agent_deployments with agent_templates to include template_name
    and extracts _last_result from config_json.
    """
    clauses: List[str] = []
    params: List[Any] = []

    if opportunity_id:
        clauses.append("d.scope_id = ?")
        params.append(opportunity_id)
    if status:
        clauses.append("d.status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    rows = db.query(
        f"""
        SELECT
            d.id            AS deployment_id,
            t.name          AS template_name,
            d.scope_id,
            d.scope_type,
            d.status,
            d.created_at,
            d.config_json
        FROM agent_deployments d
        LEFT JOIN agent_templates t ON d.agent_template_id = t.id
        {where}
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        params,
    )

    result = []
    for row in rows:
        config: Dict[str, Any] = {}
        if row.get("config_json"):
            try:
                config = json.loads(row["config_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append({
            "deployment_id": row["deployment_id"],
            "template_name": row["template_name"],
            "scope_id": row["scope_id"],
            "scope_type": row["scope_type"],
            "status": row["status"],
            "created_at": row["created_at"],
            "config": {"_last_result": config.get("_last_result")},
        })
    return result


@router.get("/execution/history/{deployment_id}", tags=["execution"])
def get_execution_detail(
    deployment_id: str, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    """Return single execution detail with full config_json parsed."""
    rows = db.query(
        """
        SELECT
            d.id            AS deployment_id,
            t.name          AS template_name,
            d.scope_id,
            d.scope_type,
            d.status,
            d.created_at,
            d.config_json
        FROM agent_deployments d
        LEFT JOIN agent_templates t ON d.agent_template_id = t.id
        WHERE d.id = ?
        """,
        [deployment_id],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Deployment not found")
    row = rows[0]
    config: Dict[str, Any] = {}
    if row.get("config_json"):
        try:
            config = json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "deployment_id": row["deployment_id"],
        "template_name": row["template_name"],
        "scope_id": row["scope_id"],
        "scope_type": row["scope_type"],
        "status": row["status"],
        "created_at": row["created_at"],
        "config": config,
    }


@router.get("/execution/summary", tags=["execution"])
def get_execution_summary(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Return aggregate stats: total, by status, by template_name, last_run_at."""
    total_rows = db.query("SELECT COUNT(*) AS total FROM agent_deployments")
    total = total_rows[0]["total"] if total_rows else 0

    by_status_rows = db.query(
        "SELECT status, COUNT(*) AS count FROM agent_deployments GROUP BY status"
    )
    by_status = {r["status"]: r["count"] for r in by_status_rows}

    by_template_rows = db.query(
        """
        SELECT t.name AS template_name, COUNT(*) AS count
        FROM agent_deployments d
        LEFT JOIN agent_templates t ON d.agent_template_id = t.id
        GROUP BY t.name
        """
    )
    by_template = {(r["template_name"] or "unknown"): r["count"] for r in by_template_rows}

    last_run_rows = db.query(
        "SELECT MAX(created_at) AS last_run_at FROM agent_deployments"
    )
    last_run_at = last_run_rows[0]["last_run_at"] if last_run_rows else None

    return {
        "total_executions": total,
        "by_status": by_status,
        "by_template_name": by_template,
        "last_run_at": last_run_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Agent Reject (Sprint 6 - Approval Hardening)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/agents/deployments/{deployment_id}/reject", tags=["agents"])
def reject_deployment(
    deployment_id: str,
    reason: Optional[str] = None,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Reject an awaiting_approval deployment → canceled, storing reason."""
    runtime = AgentRuntime(db)
    try:
        return runtime.reject(deployment_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation Logs (Sprint 6)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/evaluation/logs", tags=["evaluation"])
def list_evaluation_logs(
    entity_id: Optional[str] = None,
    event_type: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: int = 100,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    """List evaluation_logs with optional filters."""
    clauses: List[str] = []
    params: List[Any] = []

    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    rows = db.query(
        f"SELECT * FROM evaluation_logs {where} ORDER BY created_at DESC LIMIT ?",
        params,
    )
    return rows


@router.get("/evaluation/summary", tags=["evaluation"])
def evaluation_summary(
    event_type: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Return event_type + outcome aggregate counts."""
    return get_event_summary(db, event_type=event_type)


# ─────────────────────────────────────────────────────────────────────────────
# Dataset Export (Sprint 6)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/export/recommendations.jsonl", tags=["export"])
def export_recommendations_jsonl_route(
    opportunity_id: Optional[str] = None,
    db: Database = Depends(get_db),
) -> Response:
    """Stream JSONL of all recommendations joined with product_catalog."""
    content = export_recommendations_jsonl(db, opportunity_id=opportunity_id)
    return Response(content=content, media_type="application/x-ndjson")


@router.get("/export/decisions.csv", tags=["export"])
def export_decisions_csv_route(db: Database = Depends(get_db)) -> Response:
    """Stream CSV of evaluation_logs with entity context."""
    content = export_decisions_csv(db)
    return Response(content=content, media_type="text/csv")


@router.get("/export/catalog.json", tags=["export"])
def export_catalog_json_route(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Return full catalog JSON with upsell/cross-sell mappings."""
    return export_catalog_json(db)


# ─── Google Integration Routes ────────────────────────────────────────────────


@router.get("/google/auth/status", tags=["google"])
def google_auth_status() -> Dict[str, Any]:
    """Return current Google credential status."""
    return get_auth_status()


@router.get("/google/auth/url", tags=["google"])
def google_auth_url(redirect_uri: str) -> Dict[str, str]:
    """Return the OAuth2 authorization URL for the browser consent flow."""
    try:
        flow = get_oauth_flow(redirect_uri)
        auth_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true"
        )
        return {"auth_url": auth_url, "state": state}
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/google/auth/callback", tags=["google"])
def google_auth_callback(code: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchange OAuth authorization code for credentials."""
    try:
        return exchange_oauth_code(code, redirect_uri)
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/google/sheets/import", tags=["google"])
def sheets_import(
    spreadsheet_id: str,
    tab: str,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Import a Google Sheet tab into the database."""
    try:
        return import_sheet_to_db(spreadsheet_id, tab, db)
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/google/sheets/export/{opportunity_id}", tags=["google"])
def sheets_export(
    opportunity_id: str,
    spreadsheet_id: str,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Export recommendations for an opportunity to a Google Sheet."""
    try:
        return export_recommendations_to_sheet(spreadsheet_id, opportunity_id, db)
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/google/docs/proposal/{opportunity_id}", tags=["google"])
def create_proposal(
    opportunity_id: str,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Generate a Google Docs proposal artifact for an opportunity."""
    opp = db.get("opportunities", opportunity_id)
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    client_name = "Client"
    if opp.get("client_id"):
        client = db.get("clients", opp["client_id"])
        if client:
            client_name = client.get("name", "Client")

    recs = db.query(
        "SELECT r.recommendation_type, r.confidence_score, r.rationale, "
        "p.name as product_name, r.target_product_id as product_id "
        "FROM recommendations r "
        "LEFT JOIN product_catalog p ON r.target_product_id = p.id "
        "WHERE r.opportunity_id = ? ORDER BY r.confidence_score DESC LIMIT 10",
        [opportunity_id],
    )

    title = f"Proposal — {client_name} ({opportunity_id[:8]})"
    try:
        return create_proposal_doc(title, opportunity_id, client_name, recs, db)
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/google/gmail/draft", tags=["google"])
def gmail_draft(
    to: str,
    subject: str,
    body: str,
    db: Database = Depends(get_db),
    cc: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Gmail draft."""
    try:
        return create_gmail_draft(to, subject, body, db, cc)
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/google/gmail/followup/{opportunity_id}", tags=["google"])
def gmail_followup(
    opportunity_id: str,
    to_email: str,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Create a follow-up Gmail draft for an opportunity."""
    try:
        return create_followup_draft(opportunity_id, to_email, db)
    except GoogleAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/google/sync/jobs", tags=["google"])
def list_sync_jobs(
    job_type: Optional[str] = None,
    db: Database = Depends(get_db),
) -> List[Dict[str, Any]]:
    """List Google sync job history."""
    filters = {"job_type": job_type} if job_type else None
    return db.list_all("google_sync_jobs", filters)


# ── Claude Reasoning ──────────────────────────────────────────────────────────

@router.get("/claude/status", tags=["claude"])
def claude_status() -> Dict[str, Any]:
    """Check whether Claude reasoning is configured and available."""
    return {"available": claude_is_available(), "model": "claude-opus-4-6"}


@router.get("/claude/explain/{opportunity_id}", tags=["claude"])
def explain_opportunity_recommendations(
    opportunity_id: str,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Use Claude to generate a coaching explanation of why specific products
    were recommended for this opportunity.
    """
    opp_rows = db.query(
        "SELECT * FROM opportunities WHERE id = ?", [opportunity_id]
    )
    if not opp_rows:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    rec_rows = db.query(
        "SELECT * FROM recommendations WHERE opportunity_id = ? ORDER BY confidence_score DESC LIMIT 10",
        [opportunity_id],
    )

    explanation = explain_recommendations(dict(opp_rows[0]), [dict(r) for r in rec_rows])
    return {"opportunity_id": opportunity_id, "explanation": explanation}


@router.post("/claude/proposal/{opportunity_id}", tags=["claude"])
def generate_claude_proposal(
    opportunity_id: str,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Use Claude to generate a full proposal narrative for this opportunity,
    incorporating the top recommendations and catalog context.
    """
    opp_rows = db.query(
        "SELECT * FROM opportunities WHERE id = ?", [opportunity_id]
    )
    if not opp_rows:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    rec_rows = db.query(
        "SELECT * FROM recommendations WHERE opportunity_id = ? ORDER BY confidence_score DESC LIMIT 10",
        [opportunity_id],
    )
    catalog_rows = db.query("SELECT * FROM product_catalog WHERE is_active = 1 LIMIT 50", [])

    narrative = draft_proposal(
        opportunity=dict(opp_rows[0]),
        recommendations=[dict(r) for r in rec_rows],
        catalog_items=[dict(c) for c in catalog_rows],
    )
    return {"opportunity_id": opportunity_id, "proposal": narrative}


@router.post("/claude/detect-needs/{opportunity_id}", tags=["claude"])
def detect_opportunity_needs(
    opportunity_id: str,
    body: Dict[str, Any],
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Use Claude to detect client need states from a conversation transcript.
    Body: { "transcript": "string" }
    """
    transcript = body.get("transcript", "")
    if not transcript:
        raise HTTPException(status_code=422, detail="transcript is required")

    need_state_rows = db.query("SELECT id, problem_name, detected_signal FROM need_states", [])
    matched_ids = detect_need_states(transcript, [dict(ns) for ns in need_state_rows])

    return {
        "opportunity_id": opportunity_id,
        "matched_need_state_ids": matched_ids,
        "count": len(matched_ids),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Gigaton Engine — Pricing Integration
# ─────────────────────────────────────────────────────────────────────────────


class PricingCostInput(BaseModel):
    """Cost breakdown forwarded to gigaton-engine for margin calculation."""
    direct_labor: float = 0.0
    indirect_labor: float = 0.0
    tooling: float = 0.0
    delivery: float = 0.0
    support: float = 0.0
    acquisition: float = 0.0
    overhead: float = 0.0


class ProductPricingRequest(BaseModel):
    """Request body for POST /pricing/quote."""
    base_price: float = Field(gt=0, description="List price or subscription fee in USD")
    pricing_type: str = Field(default="fixed", description="fixed | tiered | subscription | hybrid")
    units: int = Field(default=1, ge=1)
    discount_rate: float = Field(default=0.0, ge=0.0, le=0.30)
    contract_term_months: int = Field(default=12, ge=1)
    costs: PricingCostInput = Field(default_factory=PricingCostInput)
    min_acceptable_margin: float = Field(default=0.20, ge=0.0, le=1.0)
    target_gross_margin: float = Field(default=0.50, ge=0.0, le=1.0)


class OpportunityPricingRequest(BaseModel):
    """Cost inputs for opportunity pricing (applied uniformly to all products)."""
    discount_rate: float = Field(default=0.0, ge=0.0, le=0.30)
    contract_term_months: int = Field(default=12, ge=1)
    costs: PricingCostInput = Field(default_factory=PricingCostInput)
    product_ids: Optional[List[str]] = Field(
        default=None,
        description="Subset of product IDs to price; None = all recommendations",
    )


@router.get("/gigaton/status", tags=["gigaton"])
def gigaton_status() -> Dict[str, Any]:
    """Check whether gigaton-engine is reachable and return its URL."""
    client = get_gigaton_client()
    return client.health()


@router.post("/pricing/quote", tags=["gigaton"])
def price_quote(req: ProductPricingRequest) -> Dict[str, Any]:
    """
    Compute a margin-governed price for a single product via gigaton-engine.

    Returns recommended_price, floor_price, gross_margin, margin_warnings,
    and approval_required flag.  Returns 503 if gigaton-engine is unreachable.
    """
    client = get_gigaton_client()
    costs = CostBreakdown(
        direct_labor=req.costs.direct_labor,
        indirect_labor=req.costs.indirect_labor,
        tooling=req.costs.tooling,
        delivery=req.costs.delivery,
        support=req.costs.support,
        acquisition=req.costs.acquisition,
        overhead=req.costs.overhead,
    )
    pricing_req = PricingQuoteRequest(
        pricing_type=req.pricing_type,
        base_price=req.base_price,
        units=req.units,
        discount_rate=req.discount_rate,
        contract_term_months=req.contract_term_months,
        min_acceptable_margin=req.min_acceptable_margin,
        target_gross_margin=req.target_gross_margin,
        costs=costs,
    )
    result = client.calculate(pricing_req)
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="Gigaton Engine is unavailable. Set GIGATON_ENGINE_URL or start the engine.",
        )
    return {
        "input": {
            "base_price": req.base_price,
            "pricing_type": req.pricing_type,
            "units": req.units,
            "discount_rate": req.discount_rate,
            "total_cost": costs.total,
        },
        "pricing": result.to_dict(),
        "margin_ok": result.margin_ok,
        "margin_pct": result.margin_pct,
    }


@router.post(
    "/opportunities/{opportunity_id}/pricing",
    tags=["gigaton"],
)
def opportunity_pricing(
    opportunity_id: str,
    req: OpportunityPricingRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get margin-governed prices for all (or a subset of) recommended products
    in an opportunity, calculated via gigaton-engine.

    Each product is priced using the shared cost inputs in the request body.
    Products without a base_price in the catalog are skipped.

    Returns:
        {
            "opportunity_id": ...,
            "gigaton_engine_available": bool,
            "priced_count": int,
            "skipped_count": int,
            "quotes": [
                {
                    "product_id": ...,
                    "product_name": ...,
                    "base_price": ...,
                    "pricing": { recommended_price, gross_margin, ... } | null,
                    "margin_ok": bool | null,
                }
            ]
        }
    """
    opp = db.get("opportunities", opportunity_id)
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    # Resolve which products to price
    if req.product_ids:
        product_rows = [
            db.get("product_catalog", pid)
            for pid in req.product_ids
            if db.get("product_catalog", pid)
        ]
    else:
        # Use top recommendations for this opportunity
        rec_rows = db.query(
            "SELECT DISTINCT target_product_id FROM recommendations "
            "WHERE opportunity_id = ? AND target_product_id IS NOT NULL "
            "  AND status NOT IN ('rejected','canceled') "
            "ORDER BY confidence_score DESC LIMIT 20",
            [opportunity_id],
        )
        product_rows = [
            db.get("product_catalog", r["target_product_id"])
            for r in rec_rows
            if db.get("product_catalog", r.get("target_product_id", ""))
        ]

    if not product_rows:
        # Fall back to all active catalog products (up to 20)
        product_rows = db.list_all("product_catalog", {"is_active": 1})[:20]

    costs = CostBreakdown(
        direct_labor=req.costs.direct_labor,
        indirect_labor=req.costs.indirect_labor,
        tooling=req.costs.tooling,
        delivery=req.costs.delivery,
        support=req.costs.support,
        acquisition=req.costs.acquisition,
        overhead=req.costs.overhead,
    )

    client = get_gigaton_client()
    engine_available = client.is_available()

    quotes = []
    priced = 0
    skipped = 0

    for product in product_rows:
        if not product:
            continue

        pid = product.get("id", "")
        pname = product.get("name", pid)
        # Products may store a numeric base_price; fall back to interaction_value as proxy
        base = product.get("base_price") or (product.get("interaction_value", 1) * 500.0)

        if engine_available and base and float(base) > 0:
            result = client.quote_product(
                base_price=float(base),
                costs=costs,
                units=1,
                discount_rate=req.discount_rate,
                contract_term_months=req.contract_term_months,
            )
            if result:
                priced += 1
                quotes.append({
                    "product_id": pid,
                    "product_name": pname,
                    "base_price": float(base),
                    "pricing": result.to_dict(),
                    "margin_ok": result.margin_ok,
                })
            else:
                skipped += 1
                quotes.append({
                    "product_id": pid,
                    "product_name": pname,
                    "base_price": float(base),
                    "pricing": None,
                    "margin_ok": None,
                })
        else:
            skipped += 1
            quotes.append({
                "product_id": pid,
                "product_name": pname,
                "base_price": float(base) if base else None,
                "pricing": None,
                "margin_ok": None,
            })

    return {
        "opportunity_id": opportunity_id,
        "gigaton_engine_available": engine_available,
        "priced_count": priced,
        "skipped_count": skipped,
        "quotes": quotes,
    }
