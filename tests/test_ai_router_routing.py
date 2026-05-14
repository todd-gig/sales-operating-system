"""Tests for claude_reasoning._call routing: ai_router HTTP vs direct fallback."""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest import mock

import pytest

from app.services import claude_reasoning as cr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset envs before each test."""
    monkeypatch.delenv("DECISION_ENGINE_URL", raising=False)
    monkeypatch.delenv("SALES_OS_AI_ROUTER_DISABLED", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cr._client = None
    yield


def _ok_router_response(text: str = "router-text", audit_id: str = "audit-1") -> mock.MagicMock:
    body = json.dumps({
        "text": text,
        "audit_id": audit_id,
        "provider_used": "anthropic",
        "model_used": "claude-opus-4-7",
        "prompt_version": "test.v1",
        "schema_version": "test.v1",
        "in_tokens": 10,
        "out_tokens": 20,
        "cost_usd": 0.001,
        "latency_ms": 100,
        "fallback_chain_taken": [],
    }).encode("utf-8")
    m = mock.MagicMock()
    m.__enter__ = mock.MagicMock(return_value=m)
    m.__exit__ = mock.MagicMock(return_value=None)
    m.read.return_value = body
    return m


def test_decision_engine_url_unset_returns_none():
    assert cr._decision_engine_url() is None


def test_decision_engine_url_set_returns_url(monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example/")
    assert cr._decision_engine_url() == "https://decision.example"  # rstrip slash


def test_kill_switch_disables_routing(monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    monkeypatch.setenv("SALES_OS_AI_ROUTER_DISABLED", "1")
    assert cr._decision_engine_url() is None


def test_is_available_true_when_router_configured(monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    assert cr.is_available() is True


def test_is_available_true_when_anthropic_configured(monkeypatch):
    # only valid if anthropic package is installed in test env
    if not cr._AVAILABLE:
        pytest.skip("anthropic package not installed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert cr.is_available() is True


def test_is_available_false_when_neither(monkeypatch):
    assert cr.is_available() is False


def test_call_routes_via_router_when_url_set(monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _ok_router_response(text="hi from router")

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        text = cr._call(
            "test prompt",
            prompt_version="test.v1",
            schema_version="test_schema.v1",
        )
    assert text == "hi from router"
    assert captured["url"] == "https://decision.example/v1/ai/invoke"
    body = captured["body"]
    assert body["prompt"] == "test prompt"
    assert body["caller_engine"] == "sales-os"
    assert body["caller_function"] == "claude_reasoning._call"
    assert body["prompt_version"] == "test.v1"
    assert body["schema_version"] == "test_schema.v1"


def test_call_propagates_max_tokens_provider_model(monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data)
        return _ok_router_response()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        cr._call(
            "x",
            prompt_version="t.v1",
            schema_version="s.v1",
            max_tokens=512,
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    assert captured["body"]["max_tokens"] == 512
    assert captured["body"]["model"] == "claude-sonnet-4-6"


def test_router_http_500_falls_back_to_direct(monkeypatch):
    """When ai_router returns 500, fall back to direct Anthropic + warn."""
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    err = urllib.error.HTTPError(
        "https://decision.example/v1/ai/invoke", 500, "boom",
        hdrs=None, fp=BytesIO(b'{"detail":"server error"}'),
    )

    fake_message = mock.MagicMock()
    fake_message.content = [mock.MagicMock(text="direct-fallback-text")]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_message

    with mock.patch("urllib.request.urlopen", side_effect=err), \
         mock.patch.object(cr, "_get_client", return_value=fake_client):
        text = cr._call("x", prompt_version="t.v1", schema_version="s.v1")
    assert text == "direct-fallback-text"


def test_router_unavailable_falls_back_to_direct(monkeypatch):
    """When ai_router is unreachable, fall back to direct Anthropic."""
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    err = urllib.error.URLError("connection refused")

    fake_message = mock.MagicMock()
    fake_message.content = [mock.MagicMock(text="fallback-after-network")]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_message

    with mock.patch("urllib.request.urlopen", side_effect=err), \
         mock.patch.object(cr, "_get_client", return_value=fake_client):
        text = cr._call("x", prompt_version="t.v1", schema_version="s.v1")
    assert text == "fallback-after-network"


def test_no_router_uses_direct_anthropic(monkeypatch):
    """Without DECISION_ENGINE_URL, route directly to Anthropic SDK."""
    monkeypatch.delenv("DECISION_ENGINE_URL", raising=False)

    fake_message = mock.MagicMock()
    fake_message.content = [mock.MagicMock(text="direct-route")]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_message

    with mock.patch("urllib.request.urlopen") as urlopen_mock, \
         mock.patch.object(cr, "_get_client", return_value=fake_client):
        text = cr._call("x", prompt_version="t.v1", schema_version="s.v1")
    assert text == "direct-route"
    # urllib.request.urlopen was NOT called (no router route)
    urlopen_mock.assert_not_called()


def test_kill_switch_forces_direct(monkeypatch):
    """SALES_OS_AI_ROUTER_DISABLED forces direct even when URL is set."""
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")
    monkeypatch.setenv("SALES_OS_AI_ROUTER_DISABLED", "1")

    fake_message = mock.MagicMock()
    fake_message.content = [mock.MagicMock(text="killed-router")]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_message

    with mock.patch("urllib.request.urlopen") as urlopen_mock, \
         mock.patch.object(cr, "_get_client", return_value=fake_client):
        text = cr._call("x", prompt_version="t.v1", schema_version="s.v1")
    assert text == "killed-router"
    urlopen_mock.assert_not_called()


def test_router_audit_log_includes_audit_id(monkeypatch, caplog):
    """When routing succeeds, audit log line carries audit_id from response."""
    monkeypatch.setenv("DECISION_ENGINE_URL", "https://decision.example")

    with mock.patch(
        "urllib.request.urlopen",
        return_value=_ok_router_response(audit_id="aud-xyz-123"),
    ), caplog.at_level("INFO", logger="sales_os.llm_audit"):
        cr._call("x", prompt_version="t.v1", schema_version="s.v1")

    log_text = " ".join(r.message for r in caplog.records)
    assert "ai_router_call" in log_text
    assert "audit_id=aud-xyz-123" in log_text
