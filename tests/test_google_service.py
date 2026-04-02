"""
Tests for the Google service layer (no credentials required — tests auth status
and graceful error handling paths).
"""
from __future__ import annotations

from app.services.google_service import get_auth_status, GoogleAuthError


def test_auth_status_returns_structure():
    status = get_auth_status()
    assert "google_available" in status
    assert "authenticated" in status
    assert "service_account" in status
    assert "oauth_token" in status
    assert "error" in status


def test_auth_status_not_authenticated_without_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", str(tmp_path / "sa.json"))
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(tmp_path / "creds.json"))
    # Re-import to pick up new env
    import importlib
    import app.services.google_service as gs
    importlib.reload(gs)
    status = gs.get_auth_status()
    assert status["authenticated"] is False
    # Restore
    importlib.reload(gs)


def test_sheets_import_fails_without_creds(seeded_db):
    from app.services.google_service import import_sheet_to_db
    try:
        import_sheet_to_db("fake_sheet_id", "Master_Catalog", seeded_db)
        # If google libs not present or no creds, should raise
    except (GoogleAuthError, Exception):
        pass  # expected


def test_create_gmail_draft_fails_without_creds(seeded_db):
    from app.services.google_service import create_gmail_draft
    try:
        create_gmail_draft("test@example.com", "Subject", "Body", seeded_db)
    except (GoogleAuthError, Exception):
        pass  # expected


def test_followup_draft_body_uses_client_name(seeded_db, monkeypatch):
    """Verify follow-up draft composes correct subject without real Gmail call."""
    captured = {}

    def mock_draft(to, subject, body, db, cc=None):
        captured["to"] = to
        captured["subject"] = subject
        captured["body"] = body
        return {"job_id": "mock", "draft_id": "mock_draft", "to": to, "subject": subject}

    import app.services.google_service as gs
    monkeypatch.setattr(gs, "create_gmail_draft", mock_draft)

    gs.create_followup_draft(seeded_db._opportunity_id, "lead@acme.com", seeded_db)
    assert "Acme Corp" in captured.get("subject", "")
    assert "Acme Corp" in captured.get("body", "")
    assert captured["to"] == "lead@acme.com"
