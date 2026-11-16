import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import AuditEvent
from app.main import verify_and_process_webhook
from app.models import (
    Campaign,
    CampaignEvent,
    CampaignStatus,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.settings import settings


def sign_payload(payload_bytes: bytes, secret: str | None = None) -> str:
    key = (secret or settings.webhook_signing_secret).encode("utf-8")
    return hmac.new(key, payload_bytes, hashlib.sha256).hexdigest()


def test_scenario_7_invalid_webhook_signature_changes_no_state(db):
    target_email = "victim@example.com"
    norm = normalize_email(target_email)

    payload_data = {
        "event_id": "evt-12345",
        "event_type": "bounce",
        "email": target_email,
        "detail": "Mailbox does not exist",
    }
    raw_body = json.dumps(payload_data).encode("utf-8")

    # 1. Missing signature header
    with pytest.raises(HTTPException) as exc_missing:
        verify_and_process_webhook(raw_body, None, "127.0.0.1", db)
    assert exc_missing.value.status_code == 401

    # 2. Invalid / forged signature header
    with pytest.raises(HTTPException) as exc_invalid:
        verify_and_process_webhook(
            raw_body, "forged_signature_hex_value_00000000", "127.0.0.1", db
        )
    assert exc_invalid.value.status_code == 401

    # Verify no state was changed in the database
    suppression = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert suppression is None

    events = db.query(CampaignEvent).filter(CampaignEvent.event_key == "webhook:evt-12345").all()
    assert len(events) == 0

    # Audit event should be recorded for the rejected webhook
    audit_record = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "webhooks.rejected",
            AuditEvent.detail == "invalid_signature",
        )
    )
    assert audit_record is not None


def test_valid_webhook_bounce_and_complaint(db, sample_template):
    email = "test-bounce@example.com"
    norm = normalize_email(email)

    # Setup campaign and outbox message
    campaign = Campaign(
        name="Webhook Test",
        template_id=sample_template.id,
        consent_purpose="updates",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        contact_id=1,
        email_normalized=norm,
        idempotency_key="outbox-webhook-test",
        status=OutboxStatus.ACCEPTED,
    )
    db.add(outbox)
    db.commit()

    # Valid signed bounce webhook
    payload_data = {
        "event_id": "evt-bounce-001",
        "event_type": "bounce",
        "email": email,
        "campaign_id": campaign.id,
        "outbox_id": outbox.id,
        "detail": "550 5.1.1 User Unknown",
    }
    raw_body = json.dumps(payload_data).encode("utf-8")
    sig = sign_payload(raw_body)

    res = verify_and_process_webhook(raw_body, sig, "127.0.0.1", db)
    assert res["status"] == "processed"
    assert res["event_id"] == "evt-bounce-001"

    # Outbox message status updated to FAILED
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.FAILED
    assert outbox.last_error == "provider_bounce"

    # Global suppression created
    supp = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert supp is not None
    assert supp.reason == SuppressionReason.HARD_BOUNCE

    # CampaignEvent created
    ev = db.scalar(
        select(CampaignEvent).where(CampaignEvent.event_key == "webhook:evt-bounce-001")
    )
    assert ev is not None
    assert ev.event_type == "bounce"


def test_webhook_duplicate_replay_deduplication(db):
    email = "dedup-test@example.com"
    norm = normalize_email(email)

    payload_data = {
        "event_id": "evt-dedup-999",
        "event_type": "complaint",
        "email": email,
        "detail": "Marked as spam by user",
    }
    raw_body = json.dumps(payload_data).encode("utf-8")
    sig = sign_payload(raw_body)

    # First call: processed
    res1 = verify_and_process_webhook(raw_body, sig, "127.0.0.1", db)
    assert res1["status"] == "processed"

    # Second call (replay with same event_id): ignored as duplicate
    res2 = verify_and_process_webhook(raw_body, sig, "127.0.0.1", db)
    assert res2["status"] == "duplicate_ignored"

    # Only one suppression and one campaign event exist
    assert db.query(Suppression).filter(Suppression.email_normalized == norm).count() == 1
    assert (
        db.query(CampaignEvent).filter(CampaignEvent.event_key == "webhook:evt-dedup-999").count()
        == 1
    )
