"""
Pydantic v2 schemas for every entity in the Sales Operating System.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
import uuid
from datetime import datetime, timezone


def _uid() -> str:
    return str(uuid.uuid4())


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Product Catalog ───────────────────────────────────────────────────────────

class ProductCatalogBase(BaseModel):
    name: str
    type: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    description: Optional[str] = None
    primary_goal: Optional[str] = None
    core_value: Optional[str] = None
    interaction_value: int = 1
    marketing_influence: int = 1
    score_multiplier: float = 0.0
    funnel_stage: Optional[str] = None
    primary_channel: Optional[str] = None
    automation_potential: Optional[str] = None
    source_reference: Optional[str] = None
    is_active: int = 1


class ProductCatalogCreate(ProductCatalogBase):
    id: str = Field(default_factory=_uid)


class ProductCatalog(ProductCatalogBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Bundles ───────────────────────────────────────────────────────────────────

class BundleBase(BaseModel):
    name: str
    description: Optional[str] = None
    value_proposition: Optional[str] = None


class BundleCreate(BundleBase):
    id: str = Field(default_factory=_uid)


class Bundle(BundleBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Bundle Items ──────────────────────────────────────────────────────────────

class BundleItemBase(BaseModel):
    bundle_id: str
    product_id: str
    sequence_order: int = 0
    required: int = 1


class BundleItemCreate(BundleItemBase):
    id: str = Field(default_factory=_uid)


class BundleItem(BundleItemBase):
    id: str

    model_config = {"from_attributes": True}


# ── Need States ───────────────────────────────────────────────────────────────

class NeedStateBase(BaseModel):
    problem_name: Optional[str] = None
    detected_signal: Optional[str] = None
    severity: Optional[str] = None
    description: Optional[str] = None


class NeedStateCreate(NeedStateBase):
    id: str = Field(default_factory=_uid)


class NeedState(NeedStateBase):
    id: str

    model_config = {"from_attributes": True}


# ── Need State Products ───────────────────────────────────────────────────────

class NeedStateProductBase(BaseModel):
    need_state_id: str
    product_id: str
    priority_order: int = 1
    recommendation_reason: Optional[str] = None


class NeedStateProductCreate(NeedStateProductBase):
    id: str = Field(default_factory=_uid)


class NeedStateProduct(NeedStateProductBase):
    id: str

    model_config = {"from_attributes": True}


# ── Upsell Rules ──────────────────────────────────────────────────────────────

class UpsellRuleBase(BaseModel):
    primary_product_id: Optional[str] = None
    trigger_event: Optional[str] = None
    client_need_state_id: Optional[str] = None
    recommended_product_id: Optional[str] = None
    upsell_type: Optional[str] = None
    expected_impact: Optional[str] = None
    dependency_product_id: Optional[str] = None


class UpsellRuleCreate(UpsellRuleBase):
    id: str = Field(default_factory=_uid)


class UpsellRule(UpsellRuleBase):
    id: str

    model_config = {"from_attributes": True}


# ── Cross-Sell Rules ──────────────────────────────────────────────────────────

class CrossSellRuleBase(BaseModel):
    product_id: Optional[str] = None
    paired_product_id: Optional[str] = None
    reason: Optional[str] = None
    bundle_strength: int = 3


class CrossSellRuleCreate(CrossSellRuleBase):
    id: str = Field(default_factory=_uid)


class CrossSellRule(CrossSellRuleBase):
    id: str

    model_config = {"from_attributes": True}


# ── Clients ───────────────────────────────────────────────────────────────────

class ClientBase(BaseModel):
    name: str
    segment: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class ClientCreate(ClientBase):
    id: str = Field(default_factory=_uid)


class Client(ClientBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Opportunities ─────────────────────────────────────────────────────────────

class OpportunityBase(BaseModel):
    client_id: Optional[str] = None
    title: Optional[str] = None
    stage: Optional[str] = None
    detected_need_summary: Optional[str] = None
    owner_user_id: Optional[str] = None


class OpportunityCreate(OpportunityBase):
    id: str = Field(default_factory=_uid)


class Opportunity(OpportunityBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Recommendations ───────────────────────────────────────────────────────────

class RecommendationBase(BaseModel):
    opportunity_id: Optional[str] = None
    recommendation_type: Optional[str] = None
    target_product_id: Optional[str] = None
    confidence_score: Optional[float] = None
    rationale: Optional[str] = None
    status: Optional[str] = None


class RecommendationCreate(RecommendationBase):
    id: str = Field(default_factory=_uid)


class Recommendation(RecommendationBase):
    id: str
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Agent Templates ───────────────────────────────────────────────────────────

class AgentTemplateBase(BaseModel):
    name: str
    purpose: Optional[str] = None
    system_prompt: Optional[str] = None
    tool_policy_json: Optional[str] = None
    output_schema_json: Optional[str] = None
    approval_mode: Optional[str] = None


class AgentTemplateCreate(AgentTemplateBase):
    id: str = Field(default_factory=_uid)


class AgentTemplate(AgentTemplateBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Agent Deployments ─────────────────────────────────────────────────────────

class AgentDeploymentBase(BaseModel):
    agent_template_id: Optional[str] = None
    name: Optional[str] = None
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    status: str = "draft"
    config_json: Optional[str] = None


class AgentDeploymentCreate(AgentDeploymentBase):
    id: str = Field(default_factory=_uid)


class AgentDeployment(AgentDeploymentBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Workflow Runs ─────────────────────────────────────────────────────────────

class WorkflowRunBase(BaseModel):
    workflow_type: Optional[str] = None
    status: Optional[str] = None
    input_json: Optional[str] = None
    output_json: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_text: Optional[str] = None


class WorkflowRunCreate(WorkflowRunBase):
    id: str = Field(default_factory=_uid)


class WorkflowRun(WorkflowRunBase):
    id: str

    model_config = {"from_attributes": True}


# ── Google Sync Jobs ──────────────────────────────────────────────────────────

class GoogleSyncJobBase(BaseModel):
    job_type: Optional[str] = None
    target_google_id: Optional[str] = None
    status: Optional[str] = None
    payload_json: Optional[str] = None
    result_json: Optional[str] = None


class GoogleSyncJobCreate(GoogleSyncJobBase):
    id: str = Field(default_factory=_uid)


class GoogleSyncJob(GoogleSyncJobBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Composite / Response helpers ──────────────────────────────────────────────

class RecommendationResult(BaseModel):
    """Returned by the recommendation engine."""
    product_id: str
    product_name: Optional[str] = None
    recommendation_type: str          # upsell | cross_sell | bundle
    confidence_score: float
    rationale: str
    source_rule_id: Optional[str] = None
    bundle_id: Optional[str] = None


class CatalogImportRequest(BaseModel):
    sheet_id: str
    sheet_range: Optional[str] = "Sheet1"


class DeployAgentRequest(BaseModel):
    template_id: str
    scope_type: str
    scope_id: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class RunWorkflowRequest(BaseModel):
    workflow_type: str
    input_data: Optional[Dict[str, Any]] = None
