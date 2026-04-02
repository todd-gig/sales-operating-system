"""
Shared fixtures for Sales OS tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure project root is on path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402
from app.models.database import Database, get_db  # noqa: E402


# ─── in-memory DB fixture ─────────────────────────────────────────────────────

@pytest.fixture()
def db():
    """Fresh in-memory database with schema initialised."""
    database = Database(":memory:")
    database.init_db()
    return database


@pytest.fixture()
def seeded_db(db):
    """In-memory DB with minimal seed data for recommendation tests."""
    import uuid

    def uid():
        return str(uuid.uuid4())

    # Products
    p1 = uid()
    p2 = uid()
    p3 = uid()
    p4 = uid()
    now = "2026-01-01T00:00:00+00:00"
    for pid, name, cat in [
        (p1, "Lead Magnet", "Lead Generation & Conversion"),
        (p2, "Landing Page", "Lead Generation & Conversion"),
        (p3, "Case Study", "Sales Enablement"),
        (p4, "Sales Presentation", "Sales Enablement"),
    ]:
        db.insert("product_catalog", {
            "id": pid, "name": name, "type": "Asset", "category": cat,
            "interaction_value": 4, "marketing_influence": 4,
            "score_multiplier": 1.0, "is_active": 1,
            "created_at": now, "updated_at": now,
        })

    # Disable FK enforcement during seeding so we can insert partial test data
    db._conn.execute("PRAGMA foreign_keys = OFF")

    # Cross-sell rules
    db._conn.execute(
        "CREATE TABLE IF NOT EXISTS cross_sell_rules "
        "(id TEXT PRIMARY KEY, product_id TEXT, paired_product_id TEXT, reason TEXT, bundle_strength INTEGER)"
    )
    db._conn.execute(
        "INSERT INTO cross_sell_rules VALUES (?,?,?,?,?)",
        (uid(), p1, p2, "Value exchange converts better with a dedicated capture page", 5)
    )
    db._conn.execute(
        "INSERT INTO cross_sell_rules VALUES (?,?,?,?,?)",
        (uid(), p1, p3, "Proof increases conversion", 4)
    )
    db._conn.commit()

    # Upsell rules
    db._conn.execute(
        "CREATE TABLE IF NOT EXISTS upsell_rules "
        "(id TEXT PRIMARY KEY, primary_product_id TEXT, trigger_event TEXT, "
        "recommended_product_id TEXT, upsell_type TEXT, expected_impact TEXT, "
        "dependency_product_id TEXT, client_need_state_id TEXT)"
    )
    db._conn.execute(
        "INSERT INTO upsell_rules VALUES (?,?,?,?,?,?,?,?)",
        (uid(), p1, "Traffic with poor opt-in rate", p2, "Conversion Boost",
         "Improves conversion path", None, None)
    )
    db._conn.commit()

    # Need states
    ns1 = uid()
    db._conn.execute(
        "INSERT INTO need_states (id, problem_name, detected_signal) VALUES (?,?,?)",
        (ns1, "Low lead volume", "traffic with poor opt-in")
    )
    db._conn.execute(
        "INSERT INTO need_state_products (id, need_state_id, product_id, priority_order) VALUES (?,?,?,?)",
        (uid(), ns1, p1, 1)
    )
    db._conn.commit()

    # Bundle
    b1 = uid()
    db.insert("bundles", {
        "id": b1, "name": "Acquisition Engine",
        "value_proposition": "Top-of-funnel demand generation",
        "created_at": now, "updated_at": now,
    })
    db.insert("bundle_items", {
        "id": uid(), "bundle_id": b1, "product_id": p1,
        "sequence_order": 1, "required": 1,
    })
    db.insert("bundle_items", {
        "id": uid(), "bundle_id": b1, "product_id": p2,
        "sequence_order": 2, "required": 1,
    })

    # Client + opportunity
    c1 = uid()
    db.insert("clients", {
        "id": c1, "name": "Acme Corp", "segment": "SMB",
        "status": "active", "created_at": now, "updated_at": now,
    })
    opp1 = uid()
    db.insert("opportunities", {
        "id": opp1, "client_id": c1,
        "detected_need_summary": "Low lead volume and traffic with poor opt-in rate",
        "stage": "discovery",
        "created_at": now, "updated_at": now,
    })

    db._product_ids = {"lead_magnet": p1, "landing_page": p2,
                        "case_study": p3, "sales_presentation": p4}
    db._client_id = c1
    db._opportunity_id = opp1
    db._need_state_id = ns1
    db._bundle_id = b1
    return db


# ─── FastAPI test client fixture ──────────────────────────────────────────────

@pytest.fixture()
def client(seeded_db):
    """TestClient with seeded in-memory DB injected."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: seeded_db
    with TestClient(app) as c:
        yield c
