"""
Rules-based recommendation engine for the Sales Operating System.

Ranking logic
─────────────
- Upsell recommendations are ranked by:
    1. bundle_strength / need-state priority_order (lower = higher priority)
    2. product score_multiplier (descending)
- Cross-sell recommendations are ranked by bundle_strength (descending).
- Bundle recommendations are ranked by how many of the client's active
  need_states are satisfied by the bundle's products.
- Final combined output deduplicates on product_id, keeping the highest
  confidence_score for each product.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.database import Database
from app.models.schemas import RecommendationResult
from app.services.evaluation_logger import log_event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_opportunity(opportunity_id: str, db: Database) -> Optional[Dict[str, Any]]:
    return db.get("opportunities", opportunity_id)


def _get_product(product_id: str, db: Database) -> Optional[Dict[str, Any]]:
    return db.get("product_catalog", product_id)


def _product_name(product_id: str, db: Database) -> str:
    p = _get_product(product_id, db)
    return p["name"] if p else product_id


def _score_multiplier(product_id: str, db: Database) -> float:
    p = _get_product(product_id, db)
    if p and p.get("score_multiplier"):
        return float(p["score_multiplier"])
    return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_upsell_recommendations(
    opportunity_id: str,
    db: Database,
) -> List[RecommendationResult]:
    """
    Return ranked upsell candidates for an opportunity.

    Strategy:
    1. Load the opportunity to find its detected need summary / stage.
    2. Find matching upsell_rules where:
       - primary_product_id matches a product already on the opportunity (if any)
       - OR client_need_state_id maps to a detected need state
    3. For each rule resolve the recommended product and compute a
       confidence score based on priority_order and score_multiplier.
    """
    opp = _get_opportunity(opportunity_id, db)
    if not opp:
        return []

    # Collect any product IDs already associated with the opportunity via
    # existing recommendations (status pending/accepted acts as "current").
    existing_rows = db.query(
        "SELECT target_product_id FROM recommendations "
        "WHERE opportunity_id = ? AND status NOT IN ('rejected','canceled')",
        [opportunity_id],
    )
    existing_product_ids = {r["target_product_id"] for r in existing_rows if r["target_product_id"]}

    # All upsell rules
    rules = db.list_all("upsell_rules")

    results: List[RecommendationResult] = []
    seen: set[str] = set()

    for rule in rules:
        rec_product_id = rule.get("recommended_product_id")
        if not rec_product_id or rec_product_id in seen:
            continue

        # Rule relevance score
        base_score = 0.5

        # Boost if primary product is in existing set
        if rule.get("primary_product_id") and rule["primary_product_id"] in existing_product_ids:
            base_score += 0.2

        # Boost if need state aligns with opportunity need summary
        if rule.get("client_need_state_id") and opp.get("detected_need_summary"):
            ns = db.get("need_states", rule["client_need_state_id"])
            if ns and ns.get("problem_name"):
                summary_lower = (opp.get("detected_need_summary") or "").lower()
                if ns["problem_name"].lower() in summary_lower:
                    base_score += 0.2

        # Dependency satisfied?
        dep_id = rule.get("dependency_product_id")
        if dep_id:
            if dep_id in existing_product_ids:
                base_score += 0.1
            else:
                base_score -= 0.15  # dependency not yet met

        confidence = min(1.0, base_score * _score_multiplier(rec_product_id, db))

        results.append(
            RecommendationResult(
                product_id=rec_product_id,
                product_name=_product_name(rec_product_id, db),
                recommendation_type="upsell",
                confidence_score=round(confidence, 4),
                rationale=(
                    f"Upsell rule ({rule['id']}): trigger={rule.get('trigger_event')}, "
                    f"type={rule.get('upsell_type')}, expected_impact={rule.get('expected_impact')}"
                ),
                source_rule_id=rule["id"],
            )
        )
        seen.add(rec_product_id)

    results.sort(key=lambda r: r.confidence_score, reverse=True)
    return results


def get_cross_sell_recommendations(
    product_ids: List[str],
    db: Database,
) -> List[RecommendationResult]:
    """
    Return ranked cross-sell candidates for a list of product IDs.

    For each product_id, load cross_sell_rules and score by bundle_strength
    and the paired product's score_multiplier.
    """
    if not product_ids:
        return []

    results: List[RecommendationResult] = []
    seen: set[str] = set()
    input_set = set(product_ids)

    for pid in product_ids:
        rules = db.query(
            "SELECT * FROM cross_sell_rules WHERE product_id = ?",
            [pid],
        )
        for rule in rules:
            paired = rule.get("paired_product_id")
            if not paired or paired in input_set or paired in seen:
                continue

            strength = rule.get("bundle_strength") or 3
            confidence = min(1.0, (strength / 5.0) * _score_multiplier(paired, db))

            results.append(
                RecommendationResult(
                    product_id=paired,
                    product_name=_product_name(paired, db),
                    recommendation_type="cross_sell",
                    confidence_score=round(confidence, 4),
                    rationale=(
                        f"Cross-sell rule ({rule['id']}): reason={rule.get('reason')}, "
                        f"bundle_strength={strength}"
                    ),
                    source_rule_id=rule["id"],
                )
            )
            seen.add(paired)

    results.sort(key=lambda r: r.confidence_score, reverse=True)
    return results


def get_bundle_recommendations(
    need_state_ids: List[str],
    db: Database,
) -> List[RecommendationResult]:
    """
    Return bundles ranked by how many products in each bundle satisfy the
    given need states.
    """
    if not need_state_ids:
        return []

    # Products that satisfy the given need states
    placeholders = ",".join("?" * len(need_state_ids))
    ns_products = db.query(
        f"SELECT product_id, priority_order FROM need_state_products "
        f"WHERE need_state_id IN ({placeholders})",
        need_state_ids,
    )
    relevant_product_ids = {r["product_id"] for r in ns_products}

    if not relevant_product_ids:
        return []

    bundles = db.list_all("bundles")
    results: List[RecommendationResult] = []

    for bundle in bundles:
        items = db.query(
            "SELECT product_id FROM bundle_items WHERE bundle_id = ?",
            [bundle["id"]],
        )
        bundle_product_ids = {i["product_id"] for i in items}
        overlap = bundle_product_ids & relevant_product_ids

        if not overlap:
            continue

        total = max(len(bundle_product_ids), 1)
        coverage = len(overlap) / total
        confidence = round(min(1.0, coverage), 4)

        results.append(
            RecommendationResult(
                product_id=bundle["id"],  # bundles use their own id as product_id here
                product_name=bundle.get("name"),
                recommendation_type="bundle",
                confidence_score=confidence,
                rationale=(
                    f"Bundle '{bundle.get('name')}' covers {len(overlap)}/{total} "
                    f"products relevant to detected need states. "
                    f"Value: {bundle.get('value_proposition', '')}"
                ),
                bundle_id=bundle["id"],
            )
        )

    results.sort(key=lambda r: r.confidence_score, reverse=True)
    return results


def generate_recommendations(
    opportunity_id: str,
    db: Database,
) -> List[RecommendationResult]:
    """
    Produce a combined, de-duplicated, ranked list of recommendations for
    a given opportunity.

    Steps:
    1. Gather upsell recommendations.
    2. Infer product_ids from existing recs + opportunity need states to
       feed into cross-sell.
    3. Infer need_state_ids from opportunity for bundle recs.
    4. Merge all three lists, deduplicate by product_id (keep highest score).
    5. Persist each recommendation into the recommendations table.
    """
    opp = _get_opportunity(opportunity_id, db)
    if not opp:
        return []

    # --- Upsell ---
    upsell = get_upsell_recommendations(opportunity_id, db)

    # --- Cross-sell ---
    existing_rows = db.query(
        "SELECT DISTINCT target_product_id FROM recommendations "
        "WHERE opportunity_id = ? AND target_product_id IS NOT NULL",
        [opportunity_id],
    )
    seed_product_ids = [r["target_product_id"] for r in existing_rows]
    # also seed from upsell top picks
    seed_product_ids += [r.product_id for r in upsell[:5]]
    cross_sell = get_cross_sell_recommendations(list(set(seed_product_ids)), db)

    # --- Bundle ---
    # Map detected_need_summary text to need_state ids via problem_name match
    need_state_ids: List[str] = []
    summary = (opp.get("detected_need_summary") or "").lower()
    if summary:
        all_ns = db.list_all("need_states")
        for ns in all_ns:
            pn = (ns.get("problem_name") or "").lower()
            sig = (ns.get("detected_signal") or "").lower()
            if pn and pn in summary:
                need_state_ids.append(ns["id"])
            elif sig and sig in summary:
                need_state_ids.append(ns["id"])

    bundles = get_bundle_recommendations(need_state_ids, db)

    # --- Merge & deduplicate ---
    combined: Dict[str, RecommendationResult] = {}
    for rec in (upsell + cross_sell + bundles):
        key = rec.bundle_id if rec.recommendation_type == "bundle" else rec.product_id
        if key not in combined or rec.confidence_score > combined[key].confidence_score:
            combined[key] = rec

    final = sorted(combined.values(), key=lambda r: r.confidence_score, reverse=True)

    # --- Persist ---
    for rec in final:
        db.insert(
            "recommendations",
            {
                "id": _uid(),
                "opportunity_id": opportunity_id,
                "recommendation_type": rec.recommendation_type,
                "target_product_id": rec.product_id if rec.recommendation_type != "bundle" else None,
                "confidence_score": rec.confidence_score,
                "rationale": rec.rationale,
                "status": "pending",
                "created_at": _now(),
            },
        )

    # --- Log evaluation event ---
    log_event(
        db,
        "recommendation_generated",
        "opportunity",
        opportunity_id,
        {"count": len(final)},
        "success",
    )

    return final


# ─────────────────────────────────────────────────────────────────────────────
# Class-based facade (importable as RecommendationEngine)
# ─────────────────────────────────────────────────────────────────────────────

class RecommendationEngine:
    """Thin stateless facade over the module-level functions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsell(self, opportunity_id: str) -> List[RecommendationResult]:
        return get_upsell_recommendations(opportunity_id, self.db)

    def cross_sell(self, product_ids: List[str]) -> List[RecommendationResult]:
        return get_cross_sell_recommendations(product_ids, self.db)

    def bundles(self, need_state_ids: List[str]) -> List[RecommendationResult]:
        return get_bundle_recommendations(need_state_ids, self.db)

    def generate(self, opportunity_id: str) -> List[RecommendationResult]:
        return generate_recommendations(opportunity_id, self.db)
