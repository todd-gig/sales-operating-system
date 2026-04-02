"""
Dataset export service for the Sales Operating System.

Produces JSONL, CSV, and JSON exports for ML training and external analysis.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional

from app.models.database import Database


def export_recommendations_jsonl(
    db: Database,
    opportunity_id: Optional[str] = None,
) -> str:
    """
    Return a JSONL string of all recommendations joined with product_catalog.

    Each line is a JSON object suitable for ML training.
    Optionally filtered to a single opportunity.
    """
    params: List[Any] = []
    where = ""
    if opportunity_id:
        where = "WHERE r.opportunity_id = ?"
        params.append(opportunity_id)

    rows = db.query(
        f"""
        SELECT
            r.id              AS recommendation_id,
            r.opportunity_id,
            r.recommendation_type,
            r.confidence_score,
            r.rationale,
            r.status          AS recommendation_status,
            r.created_at      AS recommendation_created_at,
            p.id              AS product_id,
            p.name            AS product_name,
            p.type            AS product_type,
            p.category        AS product_category,
            p.funnel_stage,
            p.automation_potential,
            p.interaction_value,
            p.marketing_influence,
            p.score_multiplier
        FROM recommendations r
        LEFT JOIN product_catalog p ON r.target_product_id = p.id
        {where}
        ORDER BY r.created_at DESC
        """,
        params,
    )

    lines = [json.dumps(row) for row in rows]
    return "\n".join(lines)


def export_decisions_csv(db: Database) -> str:
    """
    Return a CSV string of evaluation_logs joined with entity context.
    """
    rows = db.query(
        """
        SELECT
            el.id,
            el.event_type,
            el.entity_type,
            el.entity_id,
            el.outcome,
            el.created_at,
            el.payload_json,
            el.metadata_json
        FROM evaluation_logs el
        ORDER BY el.created_at DESC
        """
    )

    if not rows:
        return "id,event_type,entity_type,entity_id,outcome,created_at,payload_json,metadata_json\n"

    output = io.StringIO()
    fieldnames = ["id", "event_type", "entity_type", "entity_id", "outcome",
                  "created_at", "payload_json", "metadata_json"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_catalog_json(db: Database) -> Dict[str, Any]:
    """
    Return the full product_catalog as a dict with upsell/cross-sell mappings.
    """
    products = db.list_all("product_catalog", limit=1000)

    upsell_rules = db.list_all("upsell_rules", limit=1000)
    cross_sell_rules = db.list_all("cross_sell_rules", limit=1000)

    # Build per-product mappings
    upsell_map: Dict[str, List[Dict[str, Any]]] = {}
    for rule in upsell_rules:
        pid = rule.get("primary_product_id") or "__global__"
        upsell_map.setdefault(pid, []).append(rule)

    cross_sell_map: Dict[str, List[Dict[str, Any]]] = {}
    for rule in cross_sell_rules:
        pid = rule.get("product_id")
        if pid:
            cross_sell_map.setdefault(pid, []).append(rule)

    enriched = []
    for product in products:
        pid = product["id"]
        enriched.append({
            **product,
            "upsell_rules": upsell_map.get(pid, []),
            "cross_sell_rules": cross_sell_map.get(pid, []),
        })

    return {
        "product_count": len(enriched),
        "products": enriched,
    }
