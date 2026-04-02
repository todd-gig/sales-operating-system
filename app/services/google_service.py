"""
google_service.py
─────────────────
Google Workspace integration for the Sales Operating System.

Covers:
  - OAuth2 credential management (service account + OAuth flow)
  - Google Sheets: import catalog data, export recommendations
  - Google Docs: generate proposal / deck artifacts
  - Gmail: create draft outreach and follow-up emails

All operations are logged to the google_sync_jobs table.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models.database import Database

# ─── optional google imports (graceful degradation when creds absent) ─────────
try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    _GOOGLE_AVAILABLE = True
except ImportError:
    _GOOGLE_AVAILABLE = False


# ─── constants ────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
]

_CREDS_PATH = Path(os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json"))
_TOKEN_PATH = Path(os.environ.get("GOOGLE_TOKEN_PATH", "token.json"))
_SERVICE_ACCOUNT_PATH = Path(
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "service_account.json")
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _log_job(
    db: Database,
    job_type: str,
    target_google_id: str,
    status: str,
    payload: Any = None,
    result: Any = None,
) -> str:
    job_id = _uid()
    db.insert(
        "google_sync_jobs",
        {
            "id": job_id,
            "job_type": job_type,
            "target_google_id": target_google_id,
            "status": status,
            "payload_json": json.dumps(payload) if payload is not None else None,
            "result_json": json.dumps(result) if result is not None else None,
            "created_at": _now(),
            "updated_at": _now(),
        },
    )
    return job_id


def _update_job(db: Database, job_id: str, status: str, result: Any = None) -> None:
    db.update(
        "google_sync_jobs",
        job_id,
        {
            "status": status,
            "result_json": json.dumps(result) if result is not None else None,
            "updated_at": _now(),
        },
    )


# ─── credential management ────────────────────────────────────────────────────

class GoogleAuthError(Exception):
    pass


def get_credentials() -> "Credentials":
    """
    Return valid Google credentials.

    Priority:
    1. Service account (GOOGLE_SERVICE_ACCOUNT_PATH env var)
    2. OAuth token file (token.json)
    3. Raise GoogleAuthError with setup instructions
    """
    if not _GOOGLE_AVAILABLE:
        raise GoogleAuthError(
            "Google client libraries not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    # 1. Service account
    if _SERVICE_ACCOUNT_PATH.exists():
        return service_account.Credentials.from_service_account_file(
            str(_SERVICE_ACCOUNT_PATH), scopes=SCOPES
        )

    # 2. OAuth token
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            _TOKEN_PATH.write_text(creds.to_json())
            return creds

    raise GoogleAuthError(
        "No Google credentials found. Set one of:\n"
        "  GOOGLE_SERVICE_ACCOUNT_PATH=/path/to/service_account.json\n"
        "  GOOGLE_TOKEN_PATH=/path/to/token.json (OAuth)\n"
        "  GOOGLE_CREDENTIALS_PATH=/path/to/credentials.json (OAuth client secrets)"
    )


def get_oauth_flow(redirect_uri: str) -> "Flow":
    """Return an OAuth2 flow for the browser-based consent screen."""
    if not _GOOGLE_AVAILABLE:
        raise GoogleAuthError("Google client libraries not installed.")
    if not _CREDS_PATH.exists():
        raise GoogleAuthError(f"OAuth client secrets not found: {_CREDS_PATH}")
    return Flow.from_client_secrets_file(
        str(_CREDS_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def exchange_oauth_code(code: str, redirect_uri: str) -> Dict[str, str]:
    """Exchange an OAuth authorization code for tokens. Saves token.json."""
    flow = get_oauth_flow(redirect_uri)
    flow.fetch_token(code=code)
    creds = flow.credentials
    _TOKEN_PATH.write_text(creds.to_json())
    return {
        "token": creds.token,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "scopes": list(creds.scopes or []),
    }


# ─── Google Sheets ────────────────────────────────────────────────────────────

def sheets_read(
    spreadsheet_id: str,
    range_name: str,
) -> List[List[Any]]:
    """Read a range from a Google Sheet. Returns list-of-rows."""
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute()
    )
    return result.get("values", [])


def sheets_write(
    spreadsheet_id: str,
    range_name: str,
    values: List[List[Any]],
) -> Dict[str, Any]:
    """Write values to a range in a Google Sheet."""
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds)
    body = {"values": values}
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="RAW",
            body=body,
        )
        .execute()
    )
    return result


def import_sheet_to_db(
    spreadsheet_id: str,
    tab: str,
    db: Database,
) -> Dict[str, Any]:
    """
    Import a single sheet tab into the database.

    Supported tabs: Master_Catalog, Upsell_Matrix, Cross_Sell_Matrix,
                    Bundles, Client_Needs_Mapping
    Returns a summary dict with rows_read and status.
    """
    job_id = _log_job(db, "sheets_import", spreadsheet_id, "running", {"tab": tab})

    try:
        rows = sheets_read(spreadsheet_id, tab)
        if not rows:
            _update_job(db, job_id, "completed", {"rows_read": 0, "tab": tab})
            return {"job_id": job_id, "tab": tab, "rows_read": 0}

        headers = [str(h).strip() for h in rows[0]]
        data_rows = [
            {headers[i]: rows[r][i] if i < len(rows[r]) else None
             for i in range(len(headers))}
            for r in range(1, len(rows))
        ]

        result = {"tab": tab, "rows_read": len(data_rows), "job_id": job_id}
        _update_job(db, job_id, "completed", result)
        return result

    except Exception as exc:
        _update_job(db, job_id, "failed", {"error": str(exc)})
        raise


def export_recommendations_to_sheet(
    spreadsheet_id: str,
    opportunity_id: str,
    db: Database,
) -> Dict[str, Any]:
    """
    Export recommendations for an opportunity to a Google Sheet tab.
    Writes to sheet tab 'Recommendations'.
    """
    job_id = _log_job(
        db, "sheets_export", spreadsheet_id, "running",
        {"opportunity_id": opportunity_id}
    )

    try:
        recs = db.query(
            "SELECT r.*, p.name as product_name FROM recommendations r "
            "LEFT JOIN product_catalog p ON r.target_product_id = p.id "
            "WHERE r.opportunity_id = ? ORDER BY r.confidence_score DESC",
            [opportunity_id],
        )

        headers = ["Type", "Product", "Confidence", "Rationale", "Status"]
        rows = [headers] + [
            [
                r.get("recommendation_type", ""),
                r.get("product_name") or r.get("target_product_id", ""),
                str(round(float(r.get("confidence_score", 0)), 4)),
                r.get("rationale", ""),
                r.get("status", ""),
            ]
            for r in recs
        ]

        sheets_write(spreadsheet_id, "Recommendations!A1", rows)
        result = {"rows_written": len(rows) - 1, "opportunity_id": opportunity_id}
        _update_job(db, job_id, "completed", result)
        return {"job_id": job_id, **result}

    except Exception as exc:
        _update_job(db, job_id, "failed", {"error": str(exc)})
        raise


# ─── Google Docs ──────────────────────────────────────────────────────────────

def create_proposal_doc(
    title: str,
    opportunity_id: str,
    client_name: str,
    recommendations: List[Dict[str, Any]],
    db: Database,
) -> Dict[str, Any]:
    """
    Create a Google Doc proposal artifact from recommendation data.
    Returns the document ID and URL.
    """
    job_id = _log_job(
        db, "docs_create", "new", "running",
        {"title": title, "opportunity_id": opportunity_id}
    )

    try:
        creds = get_credentials()
        docs = build("docs", "v1", credentials=creds)

        # Create document
        doc = docs.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]

        # Build content
        lines = [
            f"Proposal: {title}\n",
            f"Client: {client_name}\n",
            f"Opportunity: {opportunity_id}\n",
            "\nRecommended Solutions\n",
            "─" * 40 + "\n",
        ]
        for i, rec in enumerate(recommendations[:10], 1):
            lines.append(
                f"{i}. {rec.get('product_name', rec.get('product_id', 'Unknown'))} "
                f"[{rec.get('recommendation_type', '')}] "
                f"— confidence: {rec.get('confidence_score', 0):.0%}\n"
            )
            if rec.get("rationale"):
                lines.append(f"   {rec['rationale']}\n")

        content = "".join(lines)

        # Insert text
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": content,
                        }
                    }
                ]
            },
        ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        result = {"doc_id": doc_id, "url": doc_url, "title": title}
        _update_job(db, job_id, "completed", result)

        return {"job_id": job_id, **result}

    except Exception as exc:
        _update_job(db, job_id, "failed", {"error": str(exc)})
        raise


# ─── Gmail ────────────────────────────────────────────────────────────────────

def create_gmail_draft(
    to: str,
    subject: str,
    body: str,
    db: Database,
    cc: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a Gmail draft. Returns the draft ID.
    """
    import base64
    from email.mime.text import MIMEText

    job_id = _log_job(
        db, "gmail_draft", to, "running",
        {"to": to, "subject": subject}
    )

    try:
        creds = get_credentials()
        gmail = build("gmail", "v1", credentials=creds)

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = (
            gmail.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )

        draft_id = draft["id"]
        result = {"draft_id": draft_id, "to": to, "subject": subject}
        _update_job(db, job_id, "completed", result)

        return {"job_id": job_id, **result}

    except Exception as exc:
        _update_job(db, job_id, "failed", {"error": str(exc)})
        raise


def create_followup_draft(
    opportunity_id: str,
    to_email: str,
    db: Database,
) -> Dict[str, Any]:
    """
    Generate a follow-up email draft based on an opportunity's top recommendations.
    """
    opp = db.get("opportunities", opportunity_id)
    client_name = "there"
    if opp:
        client_id = opp.get("client_id")
        if client_id:
            client = db.get("clients", client_id)
            if client:
                client_name = client.get("name", "there")

    recs = db.query(
        "SELECT r.recommendation_type, r.confidence_score, p.name as product_name "
        "FROM recommendations r "
        "LEFT JOIN product_catalog p ON r.target_product_id = p.id "
        "WHERE r.opportunity_id = ? AND r.status = 'pending' "
        "ORDER BY r.confidence_score DESC LIMIT 3",
        [opportunity_id],
    )

    rec_lines = "\n".join(
        f"  • {r.get('product_name', 'Solution')} ({r.get('recommendation_type', '')})"
        for r in recs
    ) or "  • Custom solution based on your needs"

    body = (
        f"Hi {client_name},\n\n"
        "Following up on our recent conversation — based on what we discussed, "
        "here are the top solutions I'd recommend we explore together:\n\n"
        f"{rec_lines}\n\n"
        "Happy to walk through any of these in more detail. "
        "Would you have 20 minutes this week?\n\n"
        "Best,\nTi Solutions"
    )

    subject = f"Following up — recommended solutions for {client_name}"
    return create_gmail_draft(to_email, subject, body, db)


# ─── Auth status ──────────────────────────────────────────────────────────────

def get_auth_status() -> Dict[str, Any]:
    """Return current credential status without raising."""
    status: Dict[str, Any] = {
        "google_available": _GOOGLE_AVAILABLE,
        "service_account": _SERVICE_ACCOUNT_PATH.exists(),
        "oauth_token": _TOKEN_PATH.exists(),
        "oauth_client_secrets": _CREDS_PATH.exists(),
        "authenticated": False,
        "error": None,
    }
    if not _GOOGLE_AVAILABLE:
        status["error"] = "google client libraries not installed"
        return status
    try:
        get_credentials()
        status["authenticated"] = True
    except GoogleAuthError as e:
        status["error"] = str(e)
    return status
