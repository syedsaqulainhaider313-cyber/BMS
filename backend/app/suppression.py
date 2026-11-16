from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models import (
    CampaignRecipient,
    CampaignRecipientStatus,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.settings import settings


def make_unsubscribe_token(campaign_id: int, contact_id: int, email_normalized: str) -> str:
    payload = f"v1:{campaign_id}:{contact_id}:{email_normalized}"
    signature = hmac.new(
        settings.unsubscribe_signing_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{campaign_id}.{contact_id}.{signature}"


def verify_unsubscribe_token(
    token: str, campaign_id: int, contact_id: int, email_normalized: str
) -> bool:
    expected = make_unsubscribe_token(campaign_id, contact_id, email_normalized)
    return hmac.compare_digest(token, expected)


def upsert_global_suppression(
    db: Session,
    email: str,
    reason: SuppressionReason | str,
    source: str = "aws_sns",
    detail: str | None = None,
) -> str:
    """Inserts or updates a global suppression record using fast index-backed upsert (< 1ms)."""
    try:
        norm_email = normalize_email(email)
    except ValueError:
        norm_email = email.strip().lower()

    if isinstance(reason, str):
        mapping = {
            "bounce": SuppressionReason.HARD_BOUNCE,
            "hard_bounce": SuppressionReason.HARD_BOUNCE,
            "bounced": SuppressionReason.HARD_BOUNCE,
            "complaint": SuppressionReason.COMPLAINT,
            "complained": SuppressionReason.COMPLAINT,
            "unsubscribe": SuppressionReason.UNSUBSCRIBE,
            "manual": SuppressionReason.MANUAL,
        }
        reason_enum = mapping.get(reason.lower(), SuppressionReason.HARD_BOUNCE)
    else:
        reason_enum = reason

    dialect_name = db.bind.dialect.name if db.bind else "postgresql"
    if dialect_name == "postgresql":
        stmt = (
            postgresql.insert(Suppression)
            .values(
                email_normalized=norm_email,
                reason=reason_enum,
                source=source,
                detail=detail,
            )
            .on_conflict_do_update(
                index_elements=["email_normalized"],
                set_={
                    "reason": reason_enum,
                    "source": source,
                    "detail": detail,
                },
            )
        )
        db.execute(stmt)
    elif dialect_name == "sqlite":
        stmt = (
            sqlite.insert(Suppression)
            .values(
                email_normalized=norm_email,
                reason=reason_enum,
                source=source,
                detail=detail,
            )
            .on_conflict_do_update(
                index_elements=["email_normalized"],
                set_={
                    "reason": reason_enum,
                    "source": source,
                    "detail": detail,
                },
            )
        )
        db.execute(stmt)
    else:
        existing = db.scalar(
            select(Suppression).where(Suppression.email_normalized == norm_email)
        )
        if existing:
            existing.reason = reason_enum
            existing.source = source
            existing.detail = detail
        else:
            db.add(
                Suppression(
                    email_normalized=norm_email,
                    reason=reason_enum,
                    source=source,
                    detail=detail,
                )
            )

    return norm_email


def synchronize_recipient_state(
    db: Session,
    email_normalized: str,
    new_status: str,  # "bounced", "complained", "delivered", etc.
    campaign_id: int | None = None,
    outbox_id: int | None = None,
    error_detail: str | None = None,
) -> dict[str, int]:
    """Synchronizes matching records in campaign_recipients and outbox_messages (< 1ms)."""
    status_lower = new_status.lower()
    if status_lower in {"bounced", "bounce", "hard_bounce"}:
        cr_status = CampaignRecipientStatus.BOUNCED.value
        ob_status = OutboxStatus.BOUNCED
    elif status_lower in {"complained", "complaint"}:
        cr_status = CampaignRecipientStatus.COMPLAINED.value
        ob_status = OutboxStatus.COMPLAINED
    elif status_lower in {"delivered"}:
        cr_status = CampaignRecipientStatus.DISPATCHED.value
        ob_status = OutboxStatus.DELIVERED
    elif status_lower in {"accepted"}:
        cr_status = CampaignRecipientStatus.DISPATCHED.value
        ob_status = OutboxStatus.ACCEPTED
    else:
        cr_status = status_lower
        ob_status = OutboxStatus.FAILED

    cr_updated = 0
    ob_updated = 0
    err_str = error_detail or f"provider_{status_lower}"

    # 1. Update by outbox_id (fastest point index update)
    if outbox_id is not None:
        res_ob = db.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == outbox_id)
            .values(status=ob_status, last_error=err_str)
        )
        ob_updated = res_ob.rowcount or 0

        res_cr = db.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.outbox_id == outbox_id)
            .values(status=cr_status)
        )
        cr_updated = res_cr.rowcount or 0
        if ob_updated > 0 or cr_updated > 0:
            return {"campaign_recipients": cr_updated, "outbox_messages": ob_updated}

    # 2. Update by (campaign_id, email_normalized) composite unique index
    if campaign_id is not None:
        res_ob = db.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.campaign_id == campaign_id,
                OutboxMessage.email_normalized == email_normalized,
            )
            .values(status=ob_status, last_error=err_str)
        )
        ob_updated = res_ob.rowcount or 0

        res_cr = db.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.email_normalized == email_normalized,
            )
            .values(status=cr_status)
        )
        cr_updated = res_cr.rowcount or 0
        if ob_updated > 0 or cr_updated > 0:
            return {"campaign_recipients": cr_updated, "outbox_messages": ob_updated}

    # 3. Fallback: Update across all campaigns by email_normalized
    res_cr = db.execute(
        update(CampaignRecipient)
        .where(CampaignRecipient.email_normalized == email_normalized)
        .values(status=cr_status)
    )
    cr_updated = res_cr.rowcount or 0

    res_ob = db.execute(
        update(OutboxMessage)
        .where(OutboxMessage.email_normalized == email_normalized)
        .values(status=ob_status, last_error=err_str)
    )
    ob_updated = res_ob.rowcount or 0

    return {"campaign_recipients": cr_updated, "outbox_messages": ob_updated}
