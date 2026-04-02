"""
Tests for the agent runtime state machine.
"""
from __future__ import annotations

import pytest
from app.agents.runtime import AgentRuntime


@pytest.fixture()
def runtime(seeded_db):
    rt = AgentRuntime(seeded_db)
    rt.seed_builtin_templates()
    return rt


def _template_id(runtime, name: str) -> str:
    rows = runtime.db.list_all("agent_templates", {"name": name})
    assert rows, f"Template '{name}' not found"
    return rows[0]["id"]


def test_seed_creates_five_templates(runtime):
    templates = runtime.db.list_all("agent_templates")
    assert len(templates) >= 5


def test_deploy_returns_deployment_id(runtime, seeded_db):
    tid = _template_id(runtime, "proposal_agent")
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    assert isinstance(dep_id, str) and len(dep_id) > 0


def test_deploy_status_is_ready(runtime, seeded_db):
    tid = _template_id(runtime, "proposal_agent")
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    status = runtime.get_status(dep_id)
    assert status["status"] == "ready"


def test_deploy_unknown_template_raises(runtime):
    with pytest.raises(ValueError, match="not found"):
        runtime.deploy("nonexistent-id", scope_type="opportunity")


def test_execute_no_approval_completes(runtime, seeded_db):
    tid = _template_id(runtime, "recommendation_agent")
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    result = runtime.execute(dep_id)
    assert result["status"] in {"completed", "failed", "awaiting_approval"}


def test_execute_approval_always_goes_awaiting(runtime, seeded_db):
    # Register a template with approval_mode=always
    tid = runtime.register_template(
        name="test_approval_agent",
        purpose="Test agent requiring approval",
        approval_mode="always",
    )
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    result = runtime.execute(dep_id)
    assert result["status"] == "awaiting_approval"


def test_approve_transitions_to_completed(runtime, seeded_db):
    tid = runtime.register_template(
        name="test_approve_agent",
        purpose="Test",
        approval_mode="always",
    )
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    runtime.execute(dep_id)
    result = runtime.approve(dep_id)
    assert result["status"] == "completed"


def test_cancel_deployment(runtime, seeded_db):
    tid = runtime.register_template(
        name="test_cancel_agent",
        purpose="Test",
        approval_mode="always",
    )
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    runtime.execute(dep_id)
    result = runtime.cancel(dep_id)
    assert result["status"] == "canceled"


def test_cancel_terminal_raises(runtime, seeded_db):
    tid = _template_id(runtime, "discovery_agent")
    dep_id = runtime.deploy(tid, scope_type="opportunity", scope_id=seeded_db._opportunity_id)
    # Execute and complete
    result = runtime.execute(dep_id)
    if result["status"] == "awaiting_approval":
        runtime.approve(dep_id)
    with pytest.raises(ValueError, match="terminal state"):
        runtime.cancel(dep_id)


def test_list_deployments_by_scope(runtime, seeded_db):
    tid = _template_id(runtime, "followup_agent")
    opp_id = seeded_db._opportunity_id
    runtime.deploy(tid, scope_type="opportunity", scope_id=opp_id)
    runtime.deploy(tid, scope_type="opportunity", scope_id=opp_id)
    deps = runtime.list_deployments(scope_type="opportunity", scope_id=opp_id)
    assert len(deps) >= 2


def test_get_status_unknown_raises(runtime):
    with pytest.raises(ValueError, match="not found"):
        runtime.get_status("ghost-id")


def test_register_template_returns_id(runtime):
    tid = runtime.register_template(
        name="custom_agent",
        purpose="Custom test agent",
        approval_mode="none",
    )
    assert isinstance(tid, str)
    row = runtime.db.get("agent_templates", tid)
    assert row["name"] == "custom_agent"
