"""Tests for the Gigaton Engine pricing integration.

These tests run without a live gigaton-engine. They verify:
- GigatonPricingClient graceful degradation (None when engine unavailable)
- /gigaton/status endpoint returns proper availability flag
- /pricing/quote returns 503 when engine unavailable
- /opportunities/{id}/pricing returns degraded response when engine unavailable
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.gigaton_pricing import (
    GigatonPricingClient,
    PricingQuoteRequest,
    PricingQuoteResult,
    CostBreakdown,
    get_gigaton_client,
)


# ─── Unit tests: GigatonPricingClient ────────────────────────────────────────

class TestGigatonPricingClient:

    def test_is_available_returns_false_on_connection_error(self):
        client = GigatonPricingClient(base_url="http://localhost:19999")
        assert client.is_available() is False

    def test_calculate_returns_none_on_connection_error(self):
        client = GigatonPricingClient(base_url="http://localhost:19999")
        result = client.calculate(PricingQuoteRequest(base_price=1000.0))
        assert result is None

    def test_quote_product_returns_none_when_unavailable(self):
        client = GigatonPricingClient(base_url="http://localhost:19999")
        result = client.quote_product(base_price=5000.0)
        assert result is None

    def test_health_returns_dict_with_availability(self):
        client = GigatonPricingClient(base_url="http://localhost:19999")
        h = client.health()
        assert h["gigaton_engine_available"] is False
        assert "gigaton_engine_url" in h

    def test_calculate_success_with_mock(self):
        mock_response_data = {
            "recommended_price": 4500.0,
            "floor_price": 3000.0,
            "gross_margin": 0.40,
            "contribution_margin": 0.35,
            "discount_applied": 0.10,
            "discount_impact": -500.0,
            "margin_warnings": [],
            "approval_required": False,
        }

        import urllib.request
        from io import BytesIO
        import json

        class FakeResponse:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(mock_response_data)):
            client = GigatonPricingClient(base_url="http://fake-engine:8001")
            req = PricingQuoteRequest(base_price=5000.0, discount_rate=0.10)
            result = client.calculate(req)

        assert result is not None
        assert result.recommended_price == 4500.0
        assert result.gross_margin == 0.40
        assert result.margin_ok is True
        assert result.approval_required is False

    def test_pricing_result_to_dict(self):
        result = PricingQuoteResult(
            recommended_price=4500.0,
            floor_price=3000.0,
            gross_margin=0.40,
            contribution_margin=0.35,
            discount_applied=0.10,
            discount_impact=-500.0,
            margin_warnings=[],
            approval_required=False,
        )
        d = result.to_dict()
        assert d["recommended_price"] == 4500.0
        assert d["gross_margin"] == 0.40
        assert d["margin_warnings"] == []

    def test_cost_breakdown_total(self):
        costs = CostBreakdown(direct_labor=1000.0, overhead=500.0, support=200.0)
        assert costs.total == 1700.0

    def test_quote_products_skips_zero_price(self):
        client = GigatonPricingClient(base_url="http://localhost:19999")
        products = [
            {"id": "p1", "base_price": 0},
            {"id": "p2", "base_price": None},
            {"id": "p3"},
        ]
        results = client.quote_products(products)
        assert results["p1"] is None
        assert results["p2"] is None
        assert results["p3"] is None


# ─── API route tests ─────────────────────────────────────────────────────────

class TestGigatonRoutes:

    def test_gigaton_status_unavailable(self, client):
        """When engine is unreachable, status shows unavailable."""
        with patch(
            "app.services.gigaton_pricing.GigatonPricingClient.is_available",
            return_value=False,
        ):
            resp = client.get("/api/v1/gigaton/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gigaton_engine_available"] is False
        assert "gigaton_engine_url" in data

    def test_price_quote_503_when_engine_unavailable(self, client):
        """POST /pricing/quote returns 503 when engine is unreachable."""
        with patch(
            "app.services.gigaton_pricing.GigatonPricingClient.calculate",
            return_value=None,
        ):
            resp = client.post("/api/v1/pricing/quote", json={
                "base_price": 5000.0,
                "pricing_type": "fixed",
                "costs": {"direct_labor": 1000.0},
            })
        assert resp.status_code == 503
        assert "Gigaton Engine is unavailable" in resp.json()["detail"]

    def test_price_quote_success_with_mock(self, client):
        """POST /pricing/quote returns pricing data when engine responds."""
        mock_result = PricingQuoteResult(
            recommended_price=4500.0,
            floor_price=3000.0,
            gross_margin=0.40,
            contribution_margin=0.35,
            discount_applied=0.10,
            discount_impact=-500.0,
            margin_warnings=[],
            approval_required=False,
        )
        with patch(
            "app.services.gigaton_pricing.GigatonPricingClient.calculate",
            return_value=mock_result,
        ):
            resp = client.post("/api/v1/pricing/quote", json={
                "base_price": 5000.0,
                "pricing_type": "fixed",
                "discount_rate": 0.10,
                "costs": {"direct_labor": 1000.0, "overhead": 500.0},
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["pricing"]["recommended_price"] == 4500.0
        assert data["margin_ok"] is True
        assert data["input"]["base_price"] == 5000.0

    def test_opportunity_pricing_when_engine_unavailable(self, client, seeded_db):
        """POST /opportunities/{id}/pricing returns skipped quotes when engine is down."""
        opp_id = seeded_db._opportunity_id
        with patch(
            "app.services.gigaton_pricing.GigatonPricingClient.is_available",
            return_value=False,
        ):
            resp = client.post(f"/api/v1/opportunities/{opp_id}/pricing", json={
                "discount_rate": 0.0,
                "costs": {},
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["opportunity_id"] == opp_id
        assert data["gigaton_engine_available"] is False
        assert data["priced_count"] == 0
        assert "quotes" in data

    def test_opportunity_pricing_404_for_unknown(self, client):
        """POST /opportunities/{id}/pricing returns 404 for unknown opportunity."""
        resp = client.post("/api/v1/opportunities/nonexistent-opp/pricing", json={})
        assert resp.status_code == 404
