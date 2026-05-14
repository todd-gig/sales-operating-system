"""Gigaton Pricing Client — HTTP bridge to gigaton-engine's pricing/margin API.

The Gigaton Engine runs as a separate FastAPI service (default: localhost:8001).
This module provides a synchronous client with graceful degradation — if the engine
is unreachable, all calls return None and the caller decides how to handle it.

Configuration:
    GIGATON_ENGINE_URL   — Base URL (default: http://localhost:8001)
    GIGATON_ENGINE_TIMEOUT — Request timeout in seconds (default: 5)

Usage:
    from app.services.gigaton_pricing import GigatonPricingClient, PricingQuoteRequest
    client = GigatonPricingClient()
    result = client.calculate(PricingQuoteRequest(...))
    if result:
        print(result.recommended_price, result.gross_margin)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GIGATON_ENGINE_URL = os.environ.get("GIGATON_ENGINE_URL", "http://localhost:8001")
GIGATON_ENGINE_TIMEOUT = int(os.environ.get("GIGATON_ENGINE_TIMEOUT", "5"))


# ── Request / Response models ─────────────────────────────────────────────────

@dataclass
class CostBreakdown:
    """Cost structure for margin calculation.

    All values in USD. Any omitted costs default to 0.
    """
    direct_labor: float = 0.0
    indirect_labor: float = 0.0
    tooling: float = 0.0
    delivery: float = 0.0
    support: float = 0.0
    acquisition: float = 0.0
    overhead: float = 0.0

    def to_dict(self) -> dict:
        return {
            "direct_labor": self.direct_labor,
            "indirect_labor": self.indirect_labor,
            "tooling": self.tooling,
            "delivery": self.delivery,
            "support": self.support,
            "acquisition": self.acquisition,
            "overhead": self.overhead,
        }

    @property
    def total(self) -> float:
        return (
            self.direct_labor + self.indirect_labor + self.tooling
            + self.delivery + self.support + self.acquisition + self.overhead
        )


@dataclass
class PricingQuoteRequest:
    """Input to gigaton-engine's POST /pricing/calculate.

    pricing_type: fixed | tiered | subscription | hybrid
    base_price:   list price or subscription recurring fee
    units:        quantity (for tiered/usage pricing)
    discount_rate: proposed discount (0.0–0.30 typical)
    costs:        cost structure for margin governance
    """
    pricing_type: str = "fixed"
    base_price: float = 0.0
    setup_fee: float = 0.0
    recurring_fee: float = 0.0
    variable_fee_per_unit: float = 0.0
    units: int = 1
    discount_rate: float = 0.0
    min_acceptable_margin: float = 0.20
    target_gross_margin: float = 0.50
    target_contribution_margin: float = 0.40
    max_discount: float = 0.30
    contract_term_months: int = 12
    costs: CostBreakdown = field(default_factory=CostBreakdown)

    def to_payload(self) -> dict:
        return {
            "pricing_type": self.pricing_type,
            "base_price": self.base_price,
            "setup_fee": self.setup_fee,
            "recurring_fee": self.recurring_fee,
            "variable_fee_per_unit": self.variable_fee_per_unit,
            "tiers": [],
            "discount_rules": [],
            "min_acceptable_margin": self.min_acceptable_margin,
            "target_gross_margin": self.target_gross_margin,
            "target_contribution_margin": self.target_contribution_margin,
            "max_discount": self.max_discount,
            "contract_term_months": self.contract_term_months,
            "units": self.units,
            "discount_rate": self.discount_rate,
            "costs": self.costs.to_dict(),
        }


@dataclass
class PricingQuoteResult:
    """Result from gigaton-engine's /pricing/calculate.

    Carries upstream `assumptions[]` per CRIT-008 — never present a
    pricing recommendation downstream without the assumptions that
    produced it.
    """
    recommended_price: float
    floor_price: float
    gross_margin: float
    contribution_margin: float
    discount_applied: float
    discount_impact: float
    margin_warnings: list[str]
    approval_required: bool
    assumptions: list[str] = field(default_factory=list)

    @classmethod
    def from_response(cls, data: dict) -> "PricingQuoteResult":
        return cls(
            recommended_price=data.get("recommended_price", 0.0),
            floor_price=data.get("floor_price", 0.0),
            gross_margin=data.get("gross_margin", 0.0),
            contribution_margin=data.get("contribution_margin", 0.0),
            discount_applied=data.get("discount_applied", 0.0),
            discount_impact=data.get("discount_impact", 0.0),
            margin_warnings=data.get("margin_warnings", []),
            approval_required=data.get("approval_required", False),
            assumptions=data.get("assumptions", []),
        )

    def to_dict(self) -> dict:
        return {
            "recommended_price": self.recommended_price,
            "floor_price": self.floor_price,
            "gross_margin": round(self.gross_margin, 4),
            "contribution_margin": round(self.contribution_margin, 4),
            "discount_applied": round(self.discount_applied, 4),
            "discount_impact": round(self.discount_impact, 4),
            "margin_warnings": self.margin_warnings,
            "approval_required": self.approval_required,
            "assumptions": self.assumptions,
        }

    @property
    def margin_ok(self) -> bool:
        """True if margin warnings are empty and approval is not required."""
        return not self.margin_warnings and not self.approval_required

    @property
    def margin_pct(self) -> str:
        return f"{self.gross_margin:.1%}"


# ── Client ────────────────────────────────────────────────────────────────────

class GigatonPricingClient:
    """Synchronous HTTP client for gigaton-engine's pricing API.

    All calls are wrapped in try/except — a down or unreachable gigaton-engine
    will return None, never raise. Callers can check `is_available()` first if
    they want to gate UI elements.
    """

    def __init__(
        self,
        base_url: str = GIGATON_ENGINE_URL,
        timeout: int = GIGATON_ENGINE_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── Core calculation ──────────────────────────────────────────────────────

    def calculate(self, req: PricingQuoteRequest) -> Optional[PricingQuoteResult]:
        """Call POST /pricing/calculate. Returns None if engine is unreachable."""
        url = f"{self.base_url}/pricing/calculate"
        payload = json.dumps(req.to_payload()).encode()

        try:
            http_req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(http_req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
                return PricingQuoteResult.from_response(data)

        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode()
            except Exception:
                pass
            logger.warning(
                "gigaton-engine /pricing/calculate HTTP %d: %s",
                exc.code, body[:200],
            )
            return None

        except (urllib.error.URLError, OSError) as exc:
            logger.debug("gigaton-engine unreachable (%s) — pricing skipped", exc)
            return None

        except Exception as exc:
            logger.warning("Unexpected error calling gigaton-engine: %s", exc)
            return None

    def quote_product(
        self,
        *,
        base_price: float,
        costs: Optional[CostBreakdown] = None,
        units: int = 1,
        discount_rate: float = 0.0,
        pricing_type: str = "fixed",
        contract_term_months: int = 12,
    ) -> Optional[PricingQuoteResult]:
        """Convenience wrapper: quote a single product with cost inputs."""
        req = PricingQuoteRequest(
            pricing_type=pricing_type,
            base_price=base_price,
            units=units,
            discount_rate=discount_rate,
            contract_term_months=contract_term_months,
            costs=costs or CostBreakdown(),
        )
        return self.calculate(req)

    # ── Batch pricing ─────────────────────────────────────────────────────────

    def quote_products(
        self,
        products: list[dict],
        default_costs: Optional[CostBreakdown] = None,
        discount_rate: float = 0.0,
    ) -> dict[str, Optional[PricingQuoteResult]]:
        """Quote a list of products (each a dict with 'id' and 'base_price').

        Returns a mapping of product_id → PricingQuoteResult | None.
        Products without a base_price are mapped to None without calling the API.
        """
        results: dict[str, Optional[PricingQuoteResult]] = {}
        costs = default_costs or CostBreakdown()

        for product in products:
            pid = product.get("id", "")
            base = product.get("base_price")
            if not base or base <= 0:
                results[pid] = None
                continue

            results[pid] = self.quote_product(
                base_price=float(base),
                costs=costs,
                discount_rate=discount_rate,
            )

        return results

    # ── Health ────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Ping gigaton-engine /health and verify it's the right service.

        Checks that the response contains version 1.0.0 (gigaton-engine's
        declared version), not just any service returning status=ok.
        """
        url = f"{self.base_url}/health"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                # Gigaton engine health returns {"status": "ok", "version": "1.0.0"}
                return data.get("status") == "ok" and data.get("version") == "1.0.0"
        except Exception:
            return False

    def health(self) -> dict:
        """Return a status dict suitable for embedding in SalesOS health check."""
        available = self.is_available()
        return {
            "gigaton_engine_url": self.base_url,
            "gigaton_engine_available": available,
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_client: Optional[GigatonPricingClient] = None


def get_gigaton_client() -> GigatonPricingClient:
    """Return the module-level GigatonPricingClient singleton."""
    global _client
    if _client is None:
        _client = GigatonPricingClient()
    return _client
