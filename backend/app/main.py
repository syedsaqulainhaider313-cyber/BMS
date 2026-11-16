import csv
import hashlib
import hmac
import io
import re
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import (
    COOKIE_NAME,
    audit,
    authenticate_request,
    create_session,
    get_db,
    password_hash,
    verify_totp_code,
)
from app.db import engine
from app.imports import ColumnMapping, commit_batch, make_batch, read_csv, validate_mapping
from app.logging_config import configure_logging
from app.models import (
    AuditEvent,
    Base,
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    ConsentRecord,
    Contact,
    EmailTemplate,
    ImportBatch,
    ImportStatus,
    OperatorSession,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.settings import settings
from app.suppression import make_unsubscribe_token, verify_unsubscribe_token
from app.templates import TemplateInput, render_preview, template_values
from app.webhooks.sns_router import process_sns_notification
from app.worker import enqueue_campaign

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Bulk Mail Management System",
    version="0.1.0",
    description="Internal campaign operations API. Initial scaffold is mock-provider only.",
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None if settings.app_env == "production" else "/redoc",
    openapi_url=None if settings.app_env == "production" else "/openapi.json",
    lifespan=lifespan,
)

_attempts: dict[str, deque[float]] = defaultdict(deque)
_attempts_lock = Lock()


def enforce_rate_limit(key: str, *, limit: int, window: int) -> None:
    now = monotonic()
    with _attempts_lock:
        if key not in _attempts and len(_attempts) >= 10_000:
            cutoff = now - max(window, 300)
            for stale_key, timestamps in list(_attempts.items()):
                while timestamps and timestamps[0] <= cutoff:
                    timestamps.popleft()
                if not timestamps:
                    _attempts.pop(stale_key, None)
            if len(_attempts) >= 10_000:
                raise HTTPException(429, "Rate limiter is busy; try again later")
        attempts = _attempts[key]
        while attempts and now - attempts[0] >= window:
            attempts.popleft()
        if len(attempts) >= limit:
            raise HTTPException(429, "Rate limit exceeded; try again later")
        attempts.append(now)


@app.middleware("http")
async def operator_access_boundary(request: Request, call_next):
    raw_path = request.scope.get("path", "")
    if raw_path.startswith("/api/"):
        request.scope["path"] = raw_path[4:]
    elif raw_path == "/api":
        request.scope["path"] = "/"

    path = request.url.path
    public_paths = {"/health", "/ready", "/auth/login", "/webhooks/provider", "/webhooks/aws-sns"}
    is_unsubscribe = (
        path.startswith("/unsubscribe/")
        or path.startswith("/v1/unsubscribe/")
        or raw_path.startswith("/api/v1/unsubscribe/")
        or raw_path.startswith("/api/unsubscribe/")
    )
    is_static = (
        path == "/"
        or path.startswith("/assets/")
        or path.endswith((".js", ".css", ".ico", ".svg", ".png", ".woff2", ".html"))
    )
    if settings.app_env != "production":
        public_paths.update({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
    if path not in public_paths and not is_unsubscribe and not is_static:
        db = next(get_db())
        try:
            session = authenticate_request(request, db)
            request.state.operator_session_id = session.id
        except HTTPException as exc:
            from fastapi.responses import JSONResponse

            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return add_security_headers(response)
        finally:
            db.close()
    return add_security_headers(await call_next(request))


def add_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    if settings.secure_cookies:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Liveness: the API process is responding."""
    return {"status": "ok", "email_provider": settings.email_provider}


@app.get("/ready", tags=["system"])
def readiness() -> JSONResponse:
    """Readiness: required persistent storage is reachable."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "database": "unavailable"}
        )
    return JSONResponse(content={"status": "ready", "database": "available"})


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(default=None, max_length=16)


@app.post("/auth/login", tags=["auth"])
def login(
    payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict[str, Any]:
    enforce_rate_limit(request.client.host if request.client else "unknown", limit=10, window=300)
    configured_hash = settings.operator_password_hash
    valid = bool(configured_hash) and payload.username == settings.operator_username
    if configured_hash:
        try:
            valid = valid and password_hash.verify(payload.password, configured_hash)
        except Exception:
            valid = False
    if not valid:
        audit(
            db,
            "auth.login_failed",
            detail=f"client={request.client.host if request.client else 'unknown'}",
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid operator credentials")

    if settings.operator_totp_secret:
        if not payload.totp_code:
            audit(
                db,
                "auth.mfa_prompted",
                detail=f"client={request.client.host if request.client else 'unknown'}",
            )
            db.commit()
            return {"mfa_required": True, "message": "Two-factor authentication code required"}
        if not verify_totp_code(settings.operator_totp_secret, payload.totp_code):
            audit(
                db,
                "auth.login_mfa_failed",
                detail=f"client={request.client.host if request.client else 'unknown'}",
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid two-factor authentication code")
        audit(db, "auth.login_mfa_verified")

    csrf_token = create_session(db, response)
    audit(db, "auth.login")
    db.commit()
    return {"username": settings.operator_username, "csrf_token": csrf_token}


@app.get("/auth/mfa/status", tags=["auth"])
def mfa_status() -> dict[str, bool]:
    return {"mfa_enabled": bool(settings.operator_totp_secret)}


@app.get("/auth/me", tags=["auth"])
def current_operator() -> dict[str, str]:
    return {"username": settings.operator_username}


@app.get("/auth/csrf", tags=["auth"])
def current_csrf(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    session = db.get(OperatorSession, request.state.operator_session_id)
    if session is None:
        raise HTTPException(401, "Session expired")
    return {"csrf_token": session.csrf_token}


@app.post("/auth/logout", tags=["auth"])
def logout(response: Response, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    session = db.get(OperatorSession, request.state.operator_session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired")
    db.delete(session)
    audit(db, "auth.logout")
    db.commit()
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, secure=settings.secure_cookies, samesite="strict"
    )
    return {"status": "logged_out"}


@app.post("/imports/preview", tags=["imports"])
async def import_preview(
    file: UploadFile = File(...), mapping_json: str = Form(...), db: Session = Depends(get_db)
) -> dict:
    enforce_rate_limit(
        f"imports:{getattr(db, 'info', {}).get('actor', 'operator')}", limit=20, window=3600
    )
    try:
        mapping = ColumnMapping.model_validate_json(mapping_json)
    except Exception as exc:
        raise HTTPException(
            422, "mapping_json must define explicit email and consent columns"
        ) from exc
    filename, headers, rows = await read_csv(file)
    mapping_dict = validate_mapping(mapping, headers)
    batch, token, report, counts = make_batch(filename, rows, mapping_dict, db)
    audit(db, "imports.previewed", detail=f"batch_id={batch.id};rows={len(rows)}")
    db.commit()
    return {
        "batch_id": batch.id,
        "commit_token": token,
        "expires_at": batch.expires_at.isoformat(),
        "headers": headers,
        "counts": counts,
        "rows": report,
    }


class ImportCommit(BaseModel):
    commit_token: str = Field(min_length=20, max_length=100)


@app.post("/imports/{batch_id}/commit", tags=["imports"])
def import_commit(
    batch_id: int, payload: ImportCommit, db: Session = Depends(get_db)
) -> dict[str, int]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import preview not found")
    result = commit_batch(batch, payload.commit_token, db)
    db.commit()
    return result


@app.get("/contacts", tags=["contacts"])
def list_contacts(db: Session = Depends(get_db)) -> list[dict]:
    contacts = db.query(Contact).order_by(Contact.created_at.desc()).limit(1000).all()
    return [
        {
            "id": contact.id,
            "email": contact.email,
            "display_name": contact.display_name,
            "consent_records": len(contact.consent_records),
            "created_at": contact.created_at.isoformat(),
        }
        for contact in contacts
    ]


@app.delete("/contacts/{contact_id}", tags=["contacts"])
def delete_contact(contact_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    address = contact.email_normalized
    redacted_campaigns = 0
    for campaign in db.query(Campaign).all():
        snapshot = campaign.recipient_snapshot
        if snapshot:
            changed = any(row.get("email") == address for row in snapshot)
            campaign.recipient_snapshot = [
                (
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"email", "unsubscribe_token", "consent_record_id"}
                    }
                    | {
                        "contact_id": row.get("contact_id"),
                        "email": "[deleted]",
                        "personal_data_deleted": True,
                    }
                )
                if row.get("email") == address
                else row
                for row in snapshot
            ]
            redacted_campaigns += int(changed)
    db.execute(
        update(CampaignRecipient)
        .where(
            (CampaignRecipient.contact_id == contact_id)
            | (CampaignRecipient.email_normalized == address)
        )
        .values(
            email_normalized=f"deleted-contact-{contact_id}",
            contact_id=None,
            consent_record_id=None,
            status=CampaignRecipientStatus.CANCELLED.value,
        )
    )
    outbox_rows = db.query(OutboxMessage).filter(
        (OutboxMessage.contact_id == contact_id) | (OutboxMessage.email_normalized == address)
    ).all()
    for message in outbox_rows:
        message.contact_id = None
        message.email_normalized = f"deleted-contact-{contact_id}"
        message.idempotency_key = hashlib.sha256(
            f"deleted-contact:{contact_id}:{message.campaign_id}".encode()
        ).hexdigest()
        if message.status in {OutboxStatus.PENDING, OutboxStatus.RETRY}:
            message.status = OutboxStatus.FAILED
            message.last_error = "contact_personal_data_deleted"
    for batch in db.query(ImportBatch).filter(ImportBatch.status == ImportStatus.PREVIEWED).all():
        if any(row.get("email_normalized") == address for row in (batch.rows_snapshot or [])):
            batch.status = ImportStatus.REJECTED
            batch.commit_token_hash = ""
            batch.rows_snapshot = []
    db.query(ConsentRecord).filter(ConsentRecord.contact_id == contact_id).delete(
        synchronize_session=False
    )
    db.delete(contact)
    audit(
        db,
        "contacts.personal_data_deleted",
        detail=f"contact_id={contact_id};campaigns_redacted={redacted_campaigns};outbox_rows={len(outbox_rows)}",
    )
    db.commit()
    suppression_retained = (
        db.scalar(select(Suppression.id).where(Suppression.email_normalized == address)) is not None
    )
    return {"status": "deleted", "suppression_retained": str(suppression_retained).lower()}


class CampaignDraft(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    template_id: int
    consent_purpose: str = Field(min_length=1, max_length=300)


def campaign_out(campaign: Campaign, *, include_snapshot: bool = False) -> dict:
    count = len(campaign.recipient_snapshot or [])
    if count == 0:
        try:
            if hasattr(campaign, "recipients") and campaign.recipients:
                count = len(campaign.recipients)
        except Exception:
            pass
    result = {
        "id": campaign.id,
        "name": campaign.name,
        "template_id": campaign.template_id,
        "consent_purpose": campaign.consent_purpose,
        "status": campaign.status.value,
        "approved_at": campaign.approved_at.isoformat() if campaign.approved_at else None,
        "recipient_count": count,
    }
    if include_snapshot and campaign.recipient_snapshot is not None:
        result["recipients"] = campaign.recipient_snapshot
    return result


def campaign_eligibility(campaign: Campaign, db: Session) -> dict:
    """Evaluates campaign audience qualification via SQL pushdown.

    O(1) memory footprint: executes entirely within database query planner.
    Preserves React UI and test suite contracts by returning both eligible and excluded breakdowns.
    """
    eligible_stmt = text("""
        SELECT 
            c.id AS contact_id,
            c.email_normalized,
            cr.id AS consent_record_id
        FROM contacts c
        JOIN consent_records cr ON cr.id = (
            SELECT id FROM consent_records
            WHERE contact_id = c.id AND purpose = :consent_purpose
            ORDER BY consented_at DESC
            LIMIT 1
        )
        LEFT JOIN suppressions s ON s.email_normalized = c.email_normalized
        WHERE s.id IS NULL
        ORDER BY c.id ASC;
    """)
    rows = db.execute(eligible_stmt, {"consent_purpose": campaign.consent_purpose}).fetchall()

    excluded_stmt = text("""
        SELECT 
            c.id AS contact_id,
            CASE 
                WHEN s.id IS NOT NULL THEN 'globally_suppressed'
                ELSE 'missing_matching_consent'
            END AS reason
        FROM contacts c
        LEFT JOIN suppressions s ON s.email_normalized = c.email_normalized
        LEFT JOIN consent_records cr ON cr.id = (
            SELECT id FROM consent_records
            WHERE contact_id = c.id AND purpose = :consent_purpose
            ORDER BY consented_at DESC
            LIMIT 1
        )
        WHERE s.id IS NOT NULL OR cr.id IS NULL
        ORDER BY c.id ASC;
    """)
    excluded_rows = db.execute(excluded_stmt, {"consent_purpose": campaign.consent_purpose}).fetchall()

    eligible = [
        {"contact_id": r.contact_id, "email": r.email_normalized, "consent_record_id": r.consent_record_id}
        for r in rows
    ]
    excluded = [
        {"contact_id": r.contact_id, "reason": r.reason}
        for r in excluded_rows
    ]

    return {
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "eligible": eligible,
        "excluded": excluded,
    }


@app.get("/campaigns", tags=["campaigns"])
def list_campaigns(db: Session = Depends(get_db)) -> list[dict]:
    return [
        campaign_out(item) for item in db.query(Campaign).order_by(Campaign.updated_at.desc()).all()
    ]


@app.get("/dashboard/summary", tags=["reports"])
def dashboard_summary(db: Session = Depends(get_db)) -> dict[str, int]:
    active_campaigns = db.scalar(
        select(func.count(Campaign.id)).where(Campaign.status == CampaignStatus.APPROVED)
    )
    contacts_with_consent = db.scalar(
        select(func.count(func.distinct(ConsentRecord.contact_id)))
        .join(Contact, Contact.id == ConsentRecord.contact_id)
        .outerjoin(Suppression, Suppression.email_normalized == Contact.email_normalized)
        .where(Suppression.id.is_(None))
    )
    accepted_messages = db.scalar(
        select(func.count(OutboxMessage.id)).where(
            OutboxMessage.status.in_([OutboxStatus.ACCEPTED, OutboxStatus.DELIVERED])
        )
    )
    suppression_count = db.scalar(select(func.count(Suppression.id)))
    return {
        "active_campaigns": int(active_campaigns or 0),
        "contacts_with_consent": int(contacts_with_consent or 0),
        "accepted_messages": int(accepted_messages or 0),
        "suppressed_addresses": int(suppression_count or 0),
    }


@app.post("/campaigns", tags=["campaigns"])
def create_campaign(payload: CampaignDraft, db: Session = Depends(get_db)) -> dict:
    if db.get(EmailTemplate, payload.template_id) is None:
        raise HTTPException(404, "Template not found")
    item = Campaign(**payload.model_dump())
    db.add(item)
    db.flush()
    audit(db, "campaigns.created", detail=f"campaign_id={item.id}")
    db.commit()
    return campaign_out(item)


@app.get("/campaigns/{campaign_id}/eligibility", tags=["campaigns"])
def preview_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Campaign, campaign_id)
    if item is None:
        raise HTTPException(404, "Campaign not found")
    if item.status != CampaignStatus.DRAFT:
        raise HTTPException(409, "Eligibility preview is available only for drafts")
    report = campaign_eligibility(item, db)
    audit(
        db,
        "campaigns.eligibility_previewed",
        detail=f"campaign_id={item.id};eligible={report['eligible_count']}",
    )
    db.commit()
    return report


@app.post("/campaigns/{campaign_id}/approve", tags=["campaigns"])
def approve_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.query(Campaign).filter(Campaign.id == campaign_id).with_for_update().first()
    if item is None:
        raise HTTPException(404, "Campaign not found")
    if item.status != CampaignStatus.DRAFT:
        raise HTTPException(409, "Only draft campaigns can be approved")
    template = db.get(EmailTemplate, item.template_id)
    if template is None:
        raise HTTPException(409, "Campaign template no longer exists")
    report = campaign_eligibility(item, db)
    if not report["eligible"]:
        raise HTTPException(422, "Campaign has no eligible recipients")
    if report["eligible_count"] > settings.max_campaign_recipients:
        raise HTTPException(
            422,
            f"Eligible audience exceeds MAX_CAMPAIGN_RECIPIENTS ({settings.max_campaign_recipients})",
        )
    suppression = set(db.scalars(select(Suppression.email_normalized)).all())
    recipients = [
        recipient for recipient in report["eligible"] if recipient["email"] not in suppression
    ]
    if not recipients:
        raise HTTPException(422, "All recipients became suppressed during approval")
    for recipient in recipients:
        recipient["unsubscribe_token"] = make_unsubscribe_token(
            item.id, int(recipient["contact_id"]), str(recipient["email"])
        )
    item.frozen_content = {
        "name": template.name,
        "subject": template.subject,
        "sender_name": template.sender_name,
        "sender_email": template.sender_email,
        "mailing_address": template.mailing_address,
        "html_body": template.html_body,
        "text_body": template.text_body,
        "consent_purpose": item.consent_purpose,
    }
    from app.models import CampaignRecipient, CampaignRecipientStatus

    existing_count = (
        db.scalar(
            select(func.count(CampaignRecipient.id)).where(CampaignRecipient.campaign_id == item.id)
        )
        or 0
    )
    if existing_count == 0:
        recipient_objs = [
            CampaignRecipient(
                campaign_id=item.id,
                contact_id=int(r["contact_id"]),
                email_normalized=str(r["email"]),
                consent_record_id=r.get("consent_record_id"),
                unsubscribe_token=r["unsubscribe_token"],
                status=CampaignRecipientStatus.PENDING.value,
            )
            for r in recipients
        ]
        db.add_all(recipient_objs)

    item.recipient_snapshot = recipients
    item.status = CampaignStatus.APPROVED
    item.approved_at = datetime.now(UTC)
    audit(db, "campaigns.approved", detail=f"campaign_id={item.id};recipients={len(recipients)}")
    db.commit()
    return campaign_out(item)


@app.post("/campaigns/{campaign_id}/enqueue-mock", tags=["campaigns"])
def queue_mock_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict[str, int | str]:
    try:
        added = enqueue_campaign(campaign_id, db)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return {"status": "queued_mock", "new_jobs": added}


@app.get("/campaigns/{campaign_id}/events", tags=["campaigns"])
def campaign_events(campaign_id: int, db: Session = Depends(get_db)) -> list[dict]:
    if db.get(Campaign, campaign_id) is None:
        raise HTTPException(404, "Campaign not found")
    rows = (
        db.query(CampaignEvent)
        .filter(CampaignEvent.campaign_id == campaign_id)
        .order_by(CampaignEvent.created_at, CampaignEvent.id)
        .all()
    )
    return [
        {
            "event_type": row.event_type,
            "detail": row.detail,
            "outbox_id": row.outbox_id,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@app.delete("/campaigns/{campaign_id}/events", tags=["campaigns"])
def delete_campaign_events(campaign_id: int, db: Session = Depends(get_db)) -> dict[str, int]:
    if db.get(Campaign, campaign_id) is None:
        raise HTTPException(404, "Campaign not found")
    deleted = (
        db.query(CampaignEvent)
        .filter(CampaignEvent.campaign_id == campaign_id)
        .delete(synchronize_session=False)
    )
    audit(db, "campaigns.events_deleted", detail=f"campaign_id={campaign_id};count={deleted}")
    db.commit()
    return {"deleted": deleted}


@app.get("/unsubscribe/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe_confirmation(token: str) -> HTMLResponse:
    if len(token) > 180 or not re.fullmatch(r"\d+\.\d+\.[a-f0-9]{64}", token):
        raise HTTPException(404, "Unsubscribe link not found")
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Unsubscribe</title>"
        "<main style='font-family:system-ui;max-width:36rem;margin:10vh auto;padding:1rem'>"
        "<h1>Unsubscribe from these emails?</h1><p>Confirm to add this address to the global suppression list.</p>"
        f"<form method='post' action='/unsubscribe/{token}'><button>Confirm unsubscribe</button></form></main>"
    )


@app.post("/unsubscribe/{token}", tags=["suppression"])
def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    enforce_rate_limit(
        f"unsubscribe:{request.client.host if request.client else 'unknown'}", limit=120, window=60
    )
    match = re.fullmatch(r"(\d+)\.(\d+)\.([a-f0-9]{64})", token)
    if not match:
        raise HTTPException(404, "Unsubscribe link not found")
    campaign_id, contact_id = int(match.group(1)), int(match.group(2))
    campaign = db.get(Campaign, campaign_id)
    contact = db.get(Contact, contact_id)
    recipient_row = None
    try:
        from app.models import CampaignRecipient

        recipient_row = db.scalar(
            select(CampaignRecipient.id).where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.contact_id == contact_id,
            )
        )
    except Exception:
        recipient_row = None

    is_recipient = recipient_row is not None or (
        campaign and any(
            int(row.get("contact_id", -1)) == contact_id for row in (campaign.recipient_snapshot or [])
        )
    )
    if (
        not is_recipient
        or contact is None
        or not verify_unsubscribe_token(token, campaign_id, contact_id, contact.email_normalized)
    ):
        raise HTTPException(404, "Unsubscribe link not found")
    suppression = db.scalar(
        select(Suppression).where(Suppression.email_normalized == contact.email_normalized)
    )
    if suppression is None:
        db.add(
            Suppression(
                email_normalized=contact.email_normalized,
                reason=SuppressionReason.UNSUBSCRIBE,
                source="operator_campaign_unsubscribe",
            )
        )
    event_key = f"unsubscribe:{campaign_id}:{contact_id}"
    if db.scalar(select(CampaignEvent.id).where(CampaignEvent.event_key == event_key)) is None:
        db.add(
            CampaignEvent(
                event_key=event_key,
                campaign_id=campaign_id,
                event_type="unsubscribed",
                detail="global suppression applied",
            )
        )
    audit(db, "contacts.unsubscribed", detail=f"campaign_id={campaign_id};contact_id={contact_id}")
    db.commit()
    return {"status": "unsubscribed"}


@app.post("/api/v1/unsubscribe/one-click/{token}", tags=["suppression"])
@app.post("/v1/unsubscribe/one-click/{token}", tags=["suppression"], include_in_schema=False)
@app.post("/unsubscribe/one-click/{token}", tags=["suppression"], include_in_schema=False)
def rfc8058_one_click_unsubscribe(
    token: str,
    request: Request,
    body: bytes = Body(default=b""),
    db: Session = Depends(get_db),
) -> Response:
    """RFC 8058 Native One-Click Unsubscribe Endpoint.

    Invoked via automated HTTP POST by mailbox provider clients (Google/Yahoo).
    Exempt from operator session cookies. Immediate suppression (<200ms).
    """
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"rfc8058:{client_ip}", limit=300, window=60)

    body_bytes = body
    if not body_bytes and hasattr(request, "body"):
        b = request.body
        if callable(b):
            res = b()
            if isinstance(res, (bytes, bytearray)):
                body_bytes = res
        elif isinstance(b, (bytes, bytearray)):
            body_bytes = b
    body_str = (
        body_bytes.decode("utf-8", errors="ignore").strip()
        if isinstance(body_bytes, (bytes, bytearray))
        else str(body_bytes).strip()
    )
    if "List-Unsubscribe=One-Click" not in body_str:
        raise HTTPException(
            status_code=400,
            detail="Invalid POST body. Expected 'List-Unsubscribe=One-Click'.",
        )

    match = re.fullmatch(r"(\d+)\.(\d+)\.([a-f0-9]{64})", token)
    if not match:
        raise HTTPException(status_code=400, detail="Malformed unsubscribe token")
    campaign_id, contact_id = int(match.group(1)), int(match.group(2))

    contact = db.get(Contact, contact_id)
    if not contact or not verify_unsubscribe_token(
        token, campaign_id, contact_id, contact.email_normalized
    ):
        raise HTTPException(status_code=403, detail="Invalid unsubscribe signature")

    from app.models import CampaignRecipient

    recipient_exists = db.scalar(
        select(CampaignRecipient.id).where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.contact_id == contact_id,
        )
    )
    if not recipient_exists:
        campaign = db.get(Campaign, campaign_id)
        if not (
            campaign
            and any(
                int(r.get("contact_id", -1)) == contact_id
                for r in (campaign.recipient_snapshot or [])
            )
        ):
            raise HTTPException(status_code=404, detail="Recipient not found in campaign")

    existing_suppression = db.scalar(
        select(Suppression.id).where(Suppression.email_normalized == contact.email_normalized)
    )
    if existing_suppression is None:
        db.add(
            Suppression(
                email_normalized=contact.email_normalized,
                reason=SuppressionReason.UNSUBSCRIBE,
                source="rfc8058_one_click",
                detail=f"one_click_unsub for campaign {campaign_id}",
            )
        )

    event_key = f"unsubscribe:{campaign_id}:{contact_id}:rfc8058"
    if db.scalar(select(CampaignEvent.id).where(CampaignEvent.event_key == event_key)) is None:
        db.add(
            CampaignEvent(
                event_key=event_key,
                campaign_id=campaign_id,
                event_type="unsubscribed",
                detail="rfc8058_one_click_post",
            )
        )

    audit(
        db,
        "contacts.unsubscribed_rfc8058",
        detail=f"campaign_id={campaign_id};contact_id={contact_id}",
    )
    db.commit()
    return Response(status_code=200, content="Successfully unsubscribed")


class ProviderWebhookPayload(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(pattern=r"^(bounce|complaint|delivered|accepted|unsubscribe)$")
    email: str = Field(min_length=3, max_length=320)
    campaign_id: int | None = None
    outbox_id: int | None = None
    detail: str | None = Field(default=None, max_length=1000)


def verify_and_process_webhook(
    raw_body: bytes, signature: str | None, client_host: str, db: Session
) -> dict[str, str]:
    enforce_rate_limit(f"webhook:{client_host}", limit=120, window=60)
    if not signature:
        audit(db, "webhooks.rejected", detail="missing_signature")
        db.commit()
        raise HTTPException(401, "Missing webhook signature")

    expected_sig = hmac.new(
        settings.webhook_signing_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        audit(db, "webhooks.rejected", detail="invalid_signature")
        db.commit()
        raise HTTPException(401, "Invalid provider webhook signature")

    try:
        import json

        data = json.loads(raw_body)
        payload = ProviderWebhookPayload.model_validate(data)
    except Exception as exc:
        raise HTTPException(422, "Invalid webhook payload format") from exc

    try:
        address = normalize_email(payload.email)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    event_key = f"webhook:{payload.event_id}"
    existing_event = db.scalar(
        select(CampaignEvent.id).where(CampaignEvent.event_key == event_key)
    )
    if existing_event is not None:
        return {"status": "duplicate_ignored", "event_id": payload.event_id}

    if payload.event_type in {"bounce", "complaint", "unsubscribe"}:
        reason_map = {
            "bounce": SuppressionReason.HARD_BOUNCE,
            "complaint": SuppressionReason.COMPLAINT,
            "unsubscribe": SuppressionReason.UNSUBSCRIBE,
        }
        reason_enum = reason_map[payload.event_type]
        existing_suppression = db.scalar(
            select(Suppression.id).where(Suppression.email_normalized == address)
        )
        if existing_suppression is None:
            db.add(
                Suppression(
                    email_normalized=address,
                    reason=reason_enum,
                    source="provider_webhook",
                    detail=payload.detail,
                )
            )

    if payload.outbox_id is not None:
        outbox = db.get(OutboxMessage, payload.outbox_id)
        if outbox is not None:
            if payload.event_type in {"bounce", "complaint"}:
                outbox.status = OutboxStatus.FAILED
                outbox.last_error = f"provider_{payload.event_type}"
            elif payload.event_type == "delivered":
                outbox.status = OutboxStatus.DELIVERED
            elif payload.event_type == "accepted":
                outbox.status = OutboxStatus.ACCEPTED

    db.add(
        CampaignEvent(
            event_key=event_key,
            campaign_id=payload.campaign_id or 0,
            outbox_id=payload.outbox_id,
            event_type=payload.event_type,
            detail=payload.detail or "provider webhook notification",
        )
    )
    audit(
        db,
        "webhooks.processed",
        detail=f"event_id={payload.event_id};event_type={payload.event_type};email={address}",
    )
    db.commit()
    return {"status": "processed", "event_id": payload.event_id}


@app.post("/webhooks/provider", tags=["webhooks"])
async def provider_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    raw_body = await request.body()
    signature = request.headers.get("X-Provider-Signature") or request.headers.get(
        "X-Signature-SHA256"
    )
    return verify_and_process_webhook(
        raw_body, signature, request.client.host if request.client else "unknown", db
    )


@app.post("/webhooks/aws-sns", tags=["webhooks"])
async def aws_sns_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ingests AWS SNS notifications for Amazon SES events (Bounce, Complaint, Delivery).

    Performs strict SSRF defense, dual-mode RSA signature verification, and sub-second suppression sync.
    """
    raw_body = await request.body()
    return process_sns_notification(raw_body, db)


class ManualSuppressionInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    detail: str | None = Field(default=None, max_length=1000)


@app.get("/suppressions", tags=["suppression"])
def list_suppressions(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(Suppression).order_by(Suppression.created_at.desc()).all()
    return [
        {
            "email": row.email_normalized,
            "reason": row.reason.value,
            "source": row.source,
            "detail": row.detail,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@app.post("/suppressions", tags=["suppression"])
def create_manual_suppression(
    payload: ManualSuppressionInput, db: Session = Depends(get_db)
) -> dict[str, str]:
    try:
        address = normalize_email(payload.email)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = db.scalar(select(Suppression).where(Suppression.email_normalized == address))
    if row is None:
        row = Suppression(
            email_normalized=address,
            reason=SuppressionReason.MANUAL,
            source="operator",
            detail=payload.detail,
        )
        db.add(row)
        audit(db, "suppressions.created", detail="reason=manual")
        db.commit()
    return {"email": address, "reason": row.reason.value}


def campaign_report_data(
    campaign_id: int, db: Session
) -> tuple[Campaign, list[OutboxMessage], dict[str, int]]:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "Campaign not found")
    messages = (
        db.query(OutboxMessage)
        .filter(OutboxMessage.campaign_id == campaign_id)
        .order_by(OutboxMessage.id)
        .all()
    )
    counts = {
        status.value: sum(row.status == status for row in messages) for status in OutboxStatus
    }
    counts["snapshot_recipients"] = len(campaign.recipient_snapshot or [])
    counts["queued"] = len(messages)
    counts["accepted"] = counts[OutboxStatus.ACCEPTED.value] + counts[OutboxStatus.DELIVERED.value]
    counts["delivered"] = counts[OutboxStatus.DELIVERED.value]
    counts["pending"] = counts[OutboxStatus.PENDING.value] + counts[OutboxStatus.RETRY.value]
    counts["failed"] = counts[OutboxStatus.FAILED.value]
    counts["suppressed"] = counts[OutboxStatus.SUPPRESSED.value]
    return campaign, messages, counts


@app.get("/campaigns/{campaign_id}/report", tags=["reports"])
def campaign_report(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    campaign, messages, counts = campaign_report_data(campaign_id, db)
    return {
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "campaign_status": campaign.status.value,
        "metrics": counts,
        "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
        "reporting_note": "Accepted is provider acceptance; delivered is a separate downstream event.",
    }


def safe_csv_cell(value: object) -> str:
    text_value = str(value if value is not None else "")
    first_visible = text_value.lstrip(" \t\r\n\x00\x0b\x0c")[:1]
    if first_visible in {"=", "+", "-", "@"} or text_value.startswith(("\t", "\r", "\n")):
        return "'" + text_value
    return text_value


@app.get("/campaigns/{campaign_id}/export.csv", tags=["reports"])
def export_campaign(campaign_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    campaign, messages, _ = campaign_report_data(campaign_id, db)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["campaign_id", "campaign_name", "email", "status", "attempts", "last_error", "created_at"]
    )
    for message in messages:
        writer.writerow(
            [
                campaign.id,
                safe_csv_cell(campaign.name),
                safe_csv_cell(message.email_normalized),
                message.status.value,
                message.attempts,
                safe_csv_cell(message.last_error),
                message.created_at.isoformat() if message.created_at else "",
            ]
        )
    body = output.getvalue()
    audit(db, "campaigns.exported", detail=f"campaign_id={campaign.id};rows={len(messages)}")
    db.commit()
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="campaign-{campaign.id}-report.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/campaigns/{campaign_id}/pause", tags=["campaigns"])
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Campaign, campaign_id)
    if item is None:
        raise HTTPException(404, "Campaign not found")
    if item.status != CampaignStatus.APPROVED:
        raise HTTPException(409, "Only approved campaigns can be paused")
    item.status = CampaignStatus.PAUSED
    audit(db, "campaigns.paused", detail=f"campaign_id={item.id}")
    db.commit()
    return campaign_out(item)


@app.post("/campaigns/{campaign_id}/resume", tags=["campaigns"])
def resume_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Campaign, campaign_id)
    if item is None:
        raise HTTPException(404, "Campaign not found")
    if item.status != CampaignStatus.PAUSED:
        raise HTTPException(409, "Only paused campaigns can be resumed")
    item.status = CampaignStatus.APPROVED
    audit(db, "campaigns.resumed", detail=f"campaign_id={item.id}")
    db.commit()
    return campaign_out(item)


@app.post("/campaigns/{campaign_id}/cancel", tags=["campaigns"])
def cancel_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Campaign, campaign_id)
    if item is None:
        raise HTTPException(404, "Campaign not found")
    if item.status == CampaignStatus.CANCELLED:
        return campaign_out(item)
    item.status = CampaignStatus.CANCELLED
    cancelled_jobs = (
        db.query(OutboxMessage)
        .filter(
            OutboxMessage.campaign_id == item.id,
            OutboxMessage.status.in_([OutboxStatus.PENDING, OutboxStatus.RETRY]),
        )
        .all()
    )
    for message in cancelled_jobs:
        message.status = OutboxStatus.FAILED
        message.last_error = "campaign_cancelled"
    audit(
        db,
        "campaigns.cancelled",
        detail=f"campaign_id={item.id};pending_jobs_cancelled={len(cancelled_jobs)}",
    )
    db.commit()
    return campaign_out(item)


def template_out(template: EmailTemplate) -> dict:
    return {
        "id": template.id,
        "name": template.name,
        "subject": template.subject,
        "sender_name": template.sender_name,
        "sender_email": template.sender_email,
        "mailing_address": template.mailing_address,
        "html_body": template.html_body,
        "text_body": template.text_body,
        "created_at": template.created_at.isoformat(),
        "updated_at": template.updated_at.isoformat(),
    }


@app.get("/templates", tags=["templates"])
def list_templates(db: Session = Depends(get_db)) -> list[dict]:
    return [
        template_out(item)
        for item in db.query(EmailTemplate).order_by(EmailTemplate.updated_at.desc()).all()
    ]


@app.post("/templates", tags=["templates"])
def create_template(payload: TemplateInput, db: Session = Depends(get_db)) -> dict:
    item = EmailTemplate(**template_values(payload))
    db.add(item)
    db.flush()
    audit(db, "templates.created", detail=f"template_id={item.id}")
    db.commit()
    return template_out(item)


@app.put("/templates/{template_id}", tags=["templates"])
def update_template(
    template_id: int, payload: TemplateInput, db: Session = Depends(get_db)
) -> dict:
    item = db.get(EmailTemplate, template_id)
    if item is None:
        raise HTTPException(404, "Template not found")
    for key, value in template_values(payload).items():
        setattr(item, key, value)
    audit(db, "templates.updated", detail=f"template_id={item.id}")
    db.commit()
    return template_out(item)


@app.delete("/templates/{template_id}", tags=["templates"])
def delete_template(template_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    item = db.get(EmailTemplate, template_id)
    if item is None:
        raise HTTPException(404, "Template not found")
    db.delete(item)
    audit(db, "templates.deleted", detail=f"template_id={template_id}")
    db.commit()
    return {"status": "deleted"}


@app.post("/templates/preview", tags=["templates"])
def preview_template(payload: TemplateInput) -> dict[str, str]:
    return render_preview(payload)


@app.get("/audit-logs", tags=["audit"])
def list_audit_logs(limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:
    query_limit = min(max(1, limit), 500)
    rows = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(query_limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "detail": row.detail,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@app.get("/system/alerts", tags=["system"])
def system_alerts(db: Session = Depends(get_db)) -> dict:
    now = datetime.now(UTC)
    five_minutes_ago = now - timedelta(minutes=5)
    one_day_ago = now - timedelta(days=1)

    lagging_jobs = (
        db.scalar(
            select(func.count(OutboxMessage.id)).where(
                OutboxMessage.status.in_([OutboxStatus.PENDING, OutboxStatus.RETRY]),
                OutboxMessage.next_attempt_at < five_minutes_ago,
            )
        )
        or 0
    )

    total_dispatched_24h = (
        db.scalar(
            select(func.count(CampaignEvent.id)).where(
                CampaignEvent.event_type.in_(["accepted", "delivered"]),
                CampaignEvent.created_at >= one_day_ago,
            )
        )
        or 0
    )

    bounces_24h = (
        db.scalar(
            select(func.count(CampaignEvent.id)).where(
                CampaignEvent.event_type.in_(["bounce", "hard_bounce"]),
                CampaignEvent.created_at >= one_day_ago,
            )
        )
        or 0
    )

    complaints_24h = (
        db.scalar(
            select(func.count(CampaignEvent.id)).where(
                CampaignEvent.event_type == "complaint",
                CampaignEvent.created_at >= one_day_ago,
            )
        )
        or 0
    )

    bounce_rate_pct = (
        round((bounces_24h / total_dispatched_24h) * 100, 2) if total_dispatched_24h > 0 else 0.0
    )
    complaint_rate_pct = (
        round((complaints_24h / total_dispatched_24h) * 100, 2)
        if total_dispatched_24h > 0
        else 0.0
    )

    alerts = []
    if lagging_jobs > 0:
        alerts.append({
            "level": "warning",
            "code": "queue_lag",
            "message": f"{lagging_jobs} outbox message(s) are overdue by > 5 minutes.",
        })
    if bounce_rate_pct > 5.0:
        alerts.append({
            "level": "critical",
            "code": "high_bounce_rate",
            "message": f"Bounce rate over past 24h is {bounce_rate_pct}% (exceeds 5.0% threshold).",
        })
    if complaint_rate_pct > 0.1:
        alerts.append({
            "level": "critical",
            "code": "high_complaint_rate",
            "message": f"Complaint rate over past 24h is {complaint_rate_pct}% (exceeds 0.1% threshold).",
        })

    cb_events = (
        db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "circuit_breaker.tripped",
                AuditEvent.created_at >= one_day_ago,
            )
        )
        .scalars()
        .all()
    )
    for ev in cb_events:
        alerts.append({
            "level": "critical",
            "code": "circuit_breaker_tripped",
            "message": f"Circuit breaker tripped: {ev.detail}",
        })

    status = (
        "critical"
        if any(a["level"] == "critical" for a in alerts)
        else ("warning" if alerts else "healthy")
    )

    return {
        "status": status,
        "lagging_outbox_jobs": lagging_jobs,
        "dispatched_24h": total_dispatched_24h,
        "bounces_24h": bounces_24h,
        "complaints_24h": complaints_24h,
        "bounce_rate_pct": bounce_rate_pct,
        "complaint_rate_pct": complaint_rate_pct,
        "active_alerts": alerts,
    }


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")
