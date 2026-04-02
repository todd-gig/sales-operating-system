"""
API route tests for the Sales Operating System.
All paths use the /api/v1 prefix and actual route names from routes.py.
"""
from __future__ import annotations

import uuid


def uid():
    return str(uuid.uuid4())


# ─── health ───────────────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200


# ─── catalog / products ───────────────────────────────────────────────────────

def test_list_products(client):
    r = client.get("/api/v1/catalog/products")
    assert r.status_code == 200
    assert len(r.json()) == 4  # seeded 4 products


def test_get_product(client, seeded_db):
    pid = seeded_db._product_ids["lead_magnet"]
    r = client.get(f"/api/v1/catalog/products/{pid}")
    assert r.status_code == 200
    assert r.json()["name"] == "Lead Magnet"


def test_get_product_not_found(client):
    r = client.get("/api/v1/catalog/products/nonexistent")
    assert r.status_code == 404


def test_create_product(client):
    r = client.post("/api/v1/catalog/products", json={
        "name": "Test Widget",
        "type": "Asset",
        "category": "Brand Strategy",
        "interaction_value": 3,
        "marketing_influence": 3,
    })
    assert r.status_code in {200, 201}
    assert r.json()["name"] == "Test Widget"


# ─── bundles ──────────────────────────────────────────────────────────────────

def test_list_bundles(client):
    r = client.get("/api/v1/catalog/bundles")
    assert r.status_code == 200
    assert any(b["name"] == "Acquisition Engine" for b in r.json())


def test_get_bundle(client, seeded_db):
    r = client.get(f"/api/v1/catalog/bundles/{seeded_db._bundle_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "Acquisition Engine"


def test_get_bundle_not_found(client):
    r = client.get("/api/v1/catalog/bundles/ghost")
    assert r.status_code == 404


# ─── clients ──────────────────────────────────────────────────────────────────

def test_list_clients(client):
    r = client.get("/api/v1/clients")
    assert r.status_code == 200
    assert len(r.json()) == 1  # Acme Corp seeded


def test_create_client(client):
    r = client.post("/api/v1/clients", json={
        "name": "Beta Corp",
        "segment": "Enterprise",
        "status": "active",
    })
    assert r.status_code in {200, 201}
    assert r.json()["name"] == "Beta Corp"


def test_get_client(client, seeded_db):
    r = client.get(f"/api/v1/clients/{seeded_db._client_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "Acme Corp"


def test_get_client_not_found(client):
    r = client.get("/api/v1/clients/ghost")
    assert r.status_code == 404


# ─── opportunities ────────────────────────────────────────────────────────────

def test_list_opportunities(client):
    r = client.get("/api/v1/opportunities")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_create_opportunity(client, seeded_db):
    r = client.post("/api/v1/opportunities", json={
        "client_id": seeded_db._client_id,
        "detected_need_summary": "No clear offer",
        "stage": "discovery",
    })
    assert r.status_code in {200, 201}
    assert r.json()["stage"] == "discovery"


def test_get_opportunity(client, seeded_db):
    r = client.get(f"/api/v1/opportunities/{seeded_db._opportunity_id}")
    assert r.status_code == 200


def test_get_opportunity_not_found(client):
    r = client.get("/api/v1/opportunities/gone")
    assert r.status_code == 404


# ─── recommendations ──────────────────────────────────────────────────────────

def test_list_recommendations(client, seeded_db):
    r = client.get(f"/api/v1/opportunities/{seeded_db._opportunity_id}/recommendations")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_recommendations_for_missing_opportunity(client):
    r = client.get("/api/v1/opportunities/nope/recommendations")
    assert r.status_code == 404


# ─── google auth status ───────────────────────────────────────────────────────

def test_google_auth_status(client):
    r = client.get("/api/v1/google/auth/status")
    assert r.status_code == 200
    data = r.json()
    assert "authenticated" in data
    assert "google_available" in data


def test_google_sync_jobs_empty(client):
    r = client.get("/api/v1/google/sync/jobs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
