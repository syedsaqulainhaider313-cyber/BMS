from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import AuditEvent
from app.circuit_breaker import DeliverabilityCircuitBreaker
from app.main import system_alerts
from app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Contact,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.suppression import synchronize_recipient_state, upsert_global_suppression
from app.webhooks.sns_router import process_sns_notification
from app.webhooks.sns_verifier import (
    build_sns_canonical_bytes,
    clear_sns_certificate_cache,
    register_sns_certificate,
    validate_signing_cert_url,
    validate_subscribe_url,
    verify_sns_signature,
)
from tests._test_rsa_keys import CERT_PEM, D, N

TEST_CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-mock.pem"


@pytest.fixture(autouse=True)
def setup_test_certificates():
    """Registers deterministic test RSA certificate in cache before every test."""
    clear_sns_certificate_cache()
    register_sns_certificate(TEST_CERT_URL, CERT_PEM)
    yield
    clear_sns_certificate_cache()


def sign_mock_sns_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Cryptographically signs an SNS payload using the test 2048-bit RSA private key."""
    canonical = build_sns_canonical_bytes(payload)
    k = (N.bit_length() + 7) // 8
    sig_ver = str(payload.get("SignatureVersion", "2"))
    if sig_ver == "2":
        digest_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
        h = hashlib.sha256(canonical).digest()
    else:
        digest_prefix = bytes.fromhex("3021300906052b0e03021a05000414")
        h = hashlib.sha1(canonical).digest()
    t = digest_prefix + h
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(t)) + b"\x00" + t
    sig_int = pow(int.from_bytes(em, "big"), D, N)
    sig_bytes = sig_int.to_bytes(k, "big")
    payload["Signature"] = base64.b64encode(sig_bytes).decode("ascii")
    payload["SigningCertURL"] = TEST_CERT_URL
    return payload


def make_mock_bounce_payload(
    email: str,
    bounce_type: str = "Permanent",
    campaign_id: int | None = None,
    outbox_id: int | None = None,
    diagnostic_code: str = "550 5.1.1 User unknown",
) -> dict[str, Any]:
    ses_message = {
        "eventType": "Bounce",
        "mail": {
            "messageId": f"ses-msg-{uuid.uuid4()}",
            "destination": [email],
            "tags": {
                "campaign_id": [str(campaign_id or 1)],
                "outbox_id": [str(outbox_id or 1)],
            },
        },
        "bounce": {
            "bounceType": bounce_type,
            "bounceSubType": "General",
            "bouncedRecipients": [
                {
                    "emailAddress": email,
                    "action": "failed",
                    "status": "5.1.1" if bounce_type == "Permanent" else "4.4.1",
                    "diagnosticCode": diagnostic_code,
                }
            ],
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-ses-events",
        "Subject": "Amazon SES Email Event Notification",
        "Message": json.dumps(ses_message),
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL,
    }
    return sign_mock_sns_payload(payload)


def make_mock_complaint_payload(
    email: str,
    campaign_id: int | None = None,
    outbox_id: int | None = None,
    feedback_type: str = "abuse",
) -> dict[str, Any]:
    ses_message = {
        "eventType": "Complaint",
        "mail": {
            "messageId": f"ses-msg-{uuid.uuid4()}",
            "destination": [email],
            "tags": {
                "campaign_id": [str(campaign_id or 1)],
                "outbox_id": [str(outbox_id or 1)],
            },
        },
        "complaint": {
            "userAgent": "Mozilla/5.0",
            "complaintFeedbackType": feedback_type,
            "complainedRecipients": [{"emailAddress": email}],
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-ses-events",
        "Subject": "Amazon SES Email Event Notification",
        "Message": json.dumps(ses_message),
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL,
    }
    return sign_mock_sns_payload(payload)


def make_mock_delivery_payload(
    email: str,
    campaign_id: int | None = None,
    outbox_id: int | None = None,
) -> dict[str, Any]:
    ses_message = {
        "eventType": "Delivery",
        "mail": {
            "messageId": f"ses-msg-{uuid.uuid4()}",
            "destination": [email],
            "tags": {
                "campaign_id": [str(campaign_id or 1)],
                "outbox_id": [str(outbox_id or 1)],
            },
        },
        "delivery": {
            "timestamp": datetime.now(UTC).isoformat(),
            "processingTimeMillis": 450,
            "recipients": [email],
            "smtpResponse": "250 2.0.0 OK",
        },
    }
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-ses-events",
        "Subject": "Amazon SES Email Event Notification",
        "Message": json.dumps(ses_message),
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL,
    }
    return sign_mock_sns_payload(payload)


def make_mock_subscription_payload(
    subscribe_url: str = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=arn:aws:sns:us-east-1:123456789012:bms-events&Token=mocktok",
) -> dict[str, Any]:
    payload = {
        "Type": "SubscriptionConfirmation",
        "MessageId": str(uuid.uuid4()),
        "Token": "mocktok",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "You have chosen to subscribe to the topic.",
        "SubscribeURL": subscribe_url,
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL,
    }
    return sign_mock_sns_payload(payload)


# ==============================================================================
# R1 Tests: Webhook Ingestion, Canonical String & Cryptographic / SSRF Security
# ==============================================================================


def test_sns_canonical_bytes_building():
    payload_notif = {
        "Type": "Notification",
        "MessageId": "msg-123",
        "TopicArn": "arn:aws:sns:us-east-1:123:test",
        "Subject": "Test Notification",
        "Message": "Hello world",
        "Timestamp": "2026-11-14T10:00:00.000Z",
    }
    canonical = build_sns_canonical_bytes(payload_notif).decode("utf-8")
    expected = (
        "Message\nHello world\n"
        "MessageId\nmsg-123\n"
        "Subject\nTest Notification\n"
        "Timestamp\n2026-11-14T10:00:00.000Z\n"
        "TopicArn\narn:aws:sns:us-east-1:123:test\n"
        "Type\nNotification\n"
    )
    assert canonical == expected

    # When Subject is omitted, it should not appear in canonical string
    payload_no_subj = dict(payload_notif)
    del payload_no_subj["Subject"]
    canonical_no_subj = build_sns_canonical_bytes(payload_no_subj).decode("utf-8")
    assert "Subject" not in canonical_no_subj


def test_ssrf_protection_signing_cert_url():
    # 1. Non-HTTPS scheme
    with pytest.raises(ValueError, match="Insecure certificate scheme"):
        validate_signing_cert_url("http://sns.us-east-1.amazonaws.com/cert.pem")

    # 2. Non-AWS domain
    with pytest.raises(ValueError, match="Untrusted certificate host"):
        validate_signing_cert_url("https://attacker.com/evil.pem")

    # 3. Subdomain spoofing
    with pytest.raises(ValueError, match="Untrusted certificate host"):
        validate_signing_cert_url("https://sns.us-east-1.amazonaws.com.attacker.com/evil.pem")

    # 4. Non-pem path
    with pytest.raises(ValueError, match="Invalid certificate path"):
        validate_signing_cert_url("https://sns.us-east-1.amazonaws.com/cert.txt")

    # 5. Directory traversal in path
    with pytest.raises(ValueError, match="Invalid certificate path"):
        validate_signing_cert_url("https://sns.us-east-1.amazonaws.com/path/../cert.pem")

    # 6. Invalid port
    with pytest.raises(ValueError, match="Invalid certificate port"):
        validate_signing_cert_url("https://sns.us-east-1.amazonaws.com:8443/cert.pem")

    # 7. Valid official URLs pass
    validate_signing_cert_url("https://sns.us-east-1.amazonaws.com/SimpleNotificationService-0123.pem")
    validate_signing_cert_url("https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-4567.pem")
    validate_signing_cert_url("https://sns.cn-north-1.amazonaws.com.cn/SimpleNotificationService.pem")


def test_ssrf_protection_subscribe_url():
    # 1. Insecure scheme
    with pytest.raises(ValueError, match="Insecure subscribe scheme"):
        validate_subscribe_url("http://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription")

    # 2. Intranet metadata IP
    with pytest.raises(ValueError, match="Untrusted subscribe host"):
        validate_subscribe_url("https://169.254.169.254/confirm")

    # 3. External attacker host
    with pytest.raises(ValueError, match="Untrusted subscribe host"):
        validate_subscribe_url("https://evil.com/confirm")

    # 4. Valid amazonaws URL passes
    validate_subscribe_url("https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=123")


def test_cryptographic_verification_valid_and_tampered():
    payload = {
        "Type": "Notification",
        "MessageId": "msg-crypto-test",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:test",
        "Subject": "Crypto Test",
        "Message": '{"event": "test"}',
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL,
    }
    signed_payload = sign_mock_sns_payload(payload)

    # 1. Genuine signature passes verification
    assert verify_sns_signature(signed_payload) is True

    # 2. Tampered Message fails verification
    tampered_msg = dict(signed_payload)
    tampered_msg["Message"] = '{"event": "tampered"}'
    assert verify_sns_signature(tampered_msg) is False

    # 3. Tampered Subject fails verification
    tampered_subj = dict(signed_payload)
    tampered_subj["Subject"] = "Tampered Subject"
    assert verify_sns_signature(tampered_subj) is False

    # 4. Tampered Signature fails verification
    tampered_sig = dict(signed_payload)
    tampered_sig["Signature"] = base64.b64encode(b"invalid_signature_bytes").decode()
    assert verify_sns_signature(tampered_sig) is False


def test_webhook_endpoint_rejects_tampered_signature(db):
    payload = make_mock_bounce_payload("victim@example.com")
    payload["Signature"] = base64.b64encode(b"tampered_signature_data").decode()
    raw = json.dumps(payload).encode("utf-8")

    with pytest.raises(HTTPException) as exc:
        process_sns_notification(raw, db)
    assert exc.value.status_code == 403

    # Audit log recorded rejection
    audit_ev = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "webhooks.rejected",
            AuditEvent.detail == "invalid_sns_signature",
        )
    )
    assert audit_ev is not None


def test_subscription_confirmation_auto_confirm(db):
    payload = make_mock_subscription_payload()
    raw = json.dumps(payload).encode("utf-8")

    res = process_sns_notification(raw, db)

    assert res["status"] == "confirmed"
    assert res["mode"] == "mock"
    assert res["topic_arn"] == payload["TopicArn"]

    audit_ev = db.scalar(
        select(AuditEvent).where(AuditEvent.action == "sns.subscription_confirmed")
    )
    assert audit_ev is not None


def test_subscription_confirmation_ssrf_blocked(db):
    # Malicious subscribe url pointing to intranet
    payload = {
        "Type": "SubscriptionConfirmation",
        "MessageId": str(uuid.uuid4()),
        "Token": "tok",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:test",
        "Message": "Sub test",
        "SubscribeURL": "http://169.254.169.254/latest/meta-data/",
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL,
    }
    sign_mock_sns_payload(payload)
    raw = json.dumps(payload).encode("utf-8")

    with pytest.raises(HTTPException) as exc:
        process_sns_notification(raw, db)
    assert exc.value.status_code == 400


# ==============================================================================
# R2 Tests: Sub-Second Suppression Insertion & Recipient State Synchronization
# ==============================================================================


def test_hard_bounce_suppression_and_state_sync(db, sample_template):
    email = "hard-bounce@example.com"
    norm = normalize_email(email)

    campaign = Campaign(
        name="Phase3 Bounce Test",
        template_id=sample_template.id,
        consent_purpose="marketing",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    contact = Contact(email=email, email_normalized=norm)
    db.add(contact)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        contact_id=contact.id,
        email_normalized=norm,
        idempotency_key="outbox-hard-bounce-1",
        status=OutboxStatus.ACCEPTED,
    )
    db.add(outbox)
    db.flush()

    recip = CampaignRecipient(
        campaign_id=campaign.id,
        contact_id=contact.id,
        email_normalized=norm,
        unsubscribe_token="token-hb-1",
        status=CampaignRecipientStatus.DISPATCHED.value,
        outbox_id=outbox.id,
    )
    db.add(recip)
    db.commit()

    # Deliver signed hard bounce notification
    payload = make_mock_bounce_payload(
        email=email,
        bounce_type="Permanent",
        campaign_id=campaign.id,
        outbox_id=outbox.id,
        diagnostic_code="550 5.1.1 Mailbox does not exist",
    )
    raw = json.dumps(payload).encode("utf-8")

    res = process_sns_notification(raw, db)

    assert res["status"] == "processed"
    assert res["event_type"] == "Bounce"

    # 1. Global suppression inserted
    supp = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert supp is not None
    assert supp.reason == SuppressionReason.HARD_BOUNCE
    assert supp.source == "aws_sns"
    assert "Mailbox does not exist" in (supp.detail or "")

    # 2. State synchronized to BOUNCED
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.BOUNCED
    assert "Mailbox does not exist" in (outbox.last_error or "")

    db.refresh(recip)
    assert recip.status == CampaignRecipientStatus.BOUNCED.value

    # 3. CampaignEvent recorded
    ev = db.scalar(select(CampaignEvent).where(CampaignEvent.event_type == "hard_bounce"))
    assert ev is not None
    assert ev.campaign_id == campaign.id
    assert ev.outbox_id == outbox.id


def test_soft_bounce_does_not_suppress_and_retries(db, sample_template):
    email = "soft-bounce@example.com"
    norm = normalize_email(email)

    campaign = Campaign(
        name="Phase3 Soft Bounce Test",
        template_id=sample_template.id,
        consent_purpose="marketing",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        email_normalized=norm,
        idempotency_key="outbox-soft-bounce-1",
        status=OutboxStatus.ACCEPTED,
        attempts=0,
    )
    db.add(outbox)
    db.commit()

    # Deliver Transient soft bounce
    payload = make_mock_bounce_payload(
        email=email,
        bounce_type="Transient",
        campaign_id=campaign.id,
        outbox_id=outbox.id,
        diagnostic_code="421 4.4.1 Mailbox full",
    )
    raw = json.dumps(payload).encode("utf-8")
    res = process_sns_notification(raw, db)

    assert res["status"] == "processed"

    # 1. Global suppression MUST NOT be created
    supp = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert supp is None

    # 2. Outbox updated to RETRY with incremented attempts
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.RETRY
    assert outbox.attempts == 1
    assert "Mailbox full" in (outbox.last_error or "")

    # 3. CampaignEvent recorded as soft_bounce
    ev = db.scalar(select(CampaignEvent).where(CampaignEvent.event_type == "soft_bounce"))
    assert ev is not None


def test_complaint_suppression_and_state_sync(db, sample_template):
    email = "complaint-user@example.com"
    norm = normalize_email(email)

    campaign = Campaign(
        name="Phase3 Complaint Test",
        template_id=sample_template.id,
        consent_purpose="updates",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        email_normalized=norm,
        idempotency_key="outbox-complaint-1",
        status=OutboxStatus.ACCEPTED,
    )
    db.add(outbox)
    db.flush()

    recip = CampaignRecipient(
        campaign_id=campaign.id,
        email_normalized=norm,
        unsubscribe_token="token-complaint-1",
        status=CampaignRecipientStatus.DISPATCHED.value,
        outbox_id=outbox.id,
    )
    db.add(recip)
    db.commit()

    # Deliver signed spam complaint notification
    payload = make_mock_complaint_payload(
        email=email,
        campaign_id=campaign.id,
        outbox_id=outbox.id,
        feedback_type="abuse",
    )
    raw = json.dumps(payload).encode("utf-8")
    res = process_sns_notification(raw, db)

    assert res["status"] == "processed"
    assert res["event_type"] == "Complaint"

    # 1. Global suppression inserted
    supp = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert supp is not None
    assert supp.reason == SuppressionReason.COMPLAINT
    assert supp.source == "aws_sns"

    # 2. State synchronized to COMPLAINED
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.COMPLAINED
    assert "Spam complaint" in (outbox.last_error or "")

    db.refresh(recip)
    assert recip.status == CampaignRecipientStatus.COMPLAINED.value


def test_delivery_state_sync(db, sample_template):
    email = "delivery-success@example.com"
    norm = normalize_email(email)

    campaign = Campaign(
        name="Delivery Test",
        template_id=sample_template.id,
        consent_purpose="news",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        email_normalized=norm,
        idempotency_key="outbox-delivery-1",
        status=OutboxStatus.ACCEPTED,
    )
    db.add(outbox)
    db.commit()

    payload = make_mock_delivery_payload(email=email, campaign_id=campaign.id, outbox_id=outbox.id)
    raw = json.dumps(payload).encode("utf-8")
    res = process_sns_notification(raw, db)

    assert res["status"] == "processed"
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.DELIVERED


def test_webhook_replay_deduplication_safe(db):
    email = "dedup-sns@example.com"
    payload = make_mock_bounce_payload(email=email, bounce_type="Permanent")
    raw = json.dumps(payload).encode("utf-8")

    res1 = process_sns_notification(raw, db)
    assert res1["recipients_processed"] == 1

    # Second replay with identical MessageId and email
    res2 = process_sns_notification(raw, db)
    assert res2["recipients_processed"] == 0

    norm = normalize_email(email)
    assert db.query(Suppression).filter(Suppression.email_normalized == norm).count() == 1


def test_sub_second_latency_benchmark(db):
    """Verifies that suppression upsert + state sync completes well within the 10ms SLA."""
    email = "latency-test@example.com"
    norm = normalize_email(email)

    outbox = OutboxMessage(
        campaign_id=1,
        email_normalized=norm,
        idempotency_key="outbox-latency-key",
        status=OutboxStatus.ACCEPTED,
    )
    db.add(outbox)
    db.commit()

    t0 = time.perf_counter()
    upsert_global_suppression(db, email=email, reason=SuppressionReason.HARD_BOUNCE, source="aws_sns")
    synchronize_recipient_state(db, email_normalized=norm, new_status="bounced", outbox_id=outbox.id)
    db.commit()
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 10.0, f"Write latency {elapsed_ms:.2f}ms exceeded 10ms target"


# ==============================================================================
# R3 Tests: Automated Emergency Campaign Circuit Breaker
# ==============================================================================


def test_circuit_breaker_cold_start_guard(db, sample_template):
    campaign = Campaign(
        name="Cold Start Campaign",
        template_id=sample_template.id,
        consent_purpose="news",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.commit()

    # 10 accepted events
    now = datetime.now(UTC)
    for i in range(10):
        db.add(
            CampaignEvent(
                event_key=f"accepted-cs-{i}",
                campaign_id=campaign.id,
                event_type="accepted",
                created_at=now,
            )
        )
    # 1 hard bounce -> 1/10 = 10% (exceeds 5.0%), but volume (10) < MIN_SAMPLE_SIZE (50)
    db.add(
        CampaignEvent(
            event_key="bounce-cs-1",
            campaign_id=campaign.id,
            event_type="hard_bounce",
            created_at=now,
        )
    )
    db.commit()

    res = DeliverabilityCircuitBreaker.evaluate_campaign(db, campaign_id=campaign.id)
    assert res["status"] == "active"
    assert res["reason"] == "insufficient_volume"

    db.refresh(campaign)
    assert campaign.status == CampaignStatus.APPROVED


def test_circuit_breaker_absolute_hazard_override(db, sample_template):
    campaign = Campaign(
        name="Absolute Hazard Campaign",
        template_id=sample_template.id,
        consent_purpose="news",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        email_normalized="pending@example.com",
        idempotency_key="outbox-pending-hazard",
        status=OutboxStatus.PENDING,
    )
    db.add(outbox)
    db.commit()

    now = datetime.now(UTC)
    # Only 20 sends, but 5 spam complaints (reaches ABSOLUTE_COMPLAINTS_LIMIT = 5)
    for i in range(20):
        db.add(
            CampaignEvent(
                event_key=f"accepted-hz-{i}",
                campaign_id=campaign.id,
                event_type="accepted",
                created_at=now,
            )
        )
    for i in range(5):
        db.add(
            CampaignEvent(
                event_key=f"complaint-hz-{i}",
                campaign_id=campaign.id,
                event_type="complaint",
                created_at=now,
            )
        )
    db.commit()

    res = DeliverabilityCircuitBreaker.evaluate_campaign(db, campaign_id=campaign.id)
    assert res["status"] == "tripped"

    db.refresh(campaign)
    assert campaign.status == CampaignStatus.PAUSED

    db.refresh(outbox)
    assert outbox.status == OutboxStatus.FAILED
    assert outbox.last_error == "circuit_breaker_paused"


def test_circuit_breaker_complaint_rate_trip_and_system_alerts(db, sample_template):
    campaign = Campaign(
        name="Complaint Trip Campaign",
        template_id=sample_template.id,
        consent_purpose="marketing",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    # Create pending and retry outbox messages
    outbox_p = OutboxMessage(
        campaign_id=campaign.id,
        email_normalized="pending@example.com",
        idempotency_key="outbox-p-1",
        status=OutboxStatus.PENDING,
    )
    outbox_r = OutboxMessage(
        campaign_id=campaign.id,
        email_normalized="retry@example.com",
        idempotency_key="outbox-r-1",
        status=OutboxStatus.RETRY,
    )
    db.add_all([outbox_p, outbox_r])

    recip = CampaignRecipient(
        campaign_id=campaign.id,
        email_normalized="queued@example.com",
        unsubscribe_token="token-q-1",
        status=CampaignRecipientStatus.QUEUED.value,
    )
    db.add(recip)
    db.commit()

    now = datetime.now(UTC)
    # 1000 accepted sends
    for i in range(1000):
        db.add(
            CampaignEvent(
                event_key=f"accepted-cr-{i}",
                campaign_id=campaign.id,
                event_type="accepted",
                created_at=now,
            )
        )
    # 2 complaints -> 2 / 1000 = 0.20% > 0.10% threshold
    for i in range(2):
        db.add(
            CampaignEvent(
                event_key=f"complaint-cr-{i}",
                campaign_id=campaign.id,
                event_type="complaint",
                created_at=now,
            )
        )
    db.commit()

    res = DeliverabilityCircuitBreaker.evaluate_campaign(db, campaign_id=campaign.id)
    assert res["status"] == "tripped"
    assert "Complaint rate" in res["reason"]

    # 1. Campaign transitioned to PAUSED
    db.refresh(campaign)
    assert campaign.status == CampaignStatus.PAUSED

    # 2. Outbox messages cancelled
    db.refresh(outbox_p)
    assert outbox_p.status == OutboxStatus.FAILED
    assert outbox_p.last_error == "circuit_breaker_paused"

    db.refresh(outbox_r)
    assert outbox_r.status == OutboxStatus.FAILED
    assert outbox_r.last_error == "circuit_breaker_paused"

    # 3. Campaign recipient cancelled
    db.refresh(recip)
    assert recip.status == CampaignRecipientStatus.CANCELLED.value

    # 4. AuditEvent created with actor="system:circuit_breaker"
    audit_ev = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "circuit_breaker.tripped",
            AuditEvent.actor == "system:circuit_breaker",
        )
    )
    assert audit_ev is not None

    # 5. System alerts surfaces critical alert
    alerts_data = system_alerts(db)
    assert alerts_data["status"] == "critical"
    alert_codes = [a["code"] for a in alerts_data["active_alerts"]]
    assert "circuit_breaker_tripped" in alert_codes


def test_circuit_breaker_hard_bounce_rate_trip(db, sample_template):
    campaign = Campaign(
        name="Bounce Trip Campaign",
        template_id=sample_template.id,
        consent_purpose="marketing",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.commit()

    now = datetime.now(UTC)
    # 100 accepted sends
    for i in range(100):
        db.add(
            CampaignEvent(
                event_key=f"accepted-br-{i}",
                campaign_id=campaign.id,
                event_type="accepted",
                created_at=now,
            )
        )
    # 6 hard bounces -> 6 / 100 = 6.0% > 5.0% threshold
    for i in range(6):
        db.add(
            CampaignEvent(
                event_key=f"bounce-br-{i}",
                campaign_id=campaign.id,
                event_type="hard_bounce",
                created_at=now,
            )
        )
    db.commit()

    res = DeliverabilityCircuitBreaker.evaluate_campaign(db, campaign_id=campaign.id)
    assert res["status"] == "tripped"
    assert "Hard bounce rate" in res["reason"]

    db.refresh(campaign)
    assert campaign.status == CampaignStatus.PAUSED


def test_operator_access_boundary_public_paths():
    """Verifies /webhooks/aws-sns is registered on the FastAPI application router."""
    from app.main import app

    route_paths = [getattr(route, "path", None) for route in app.routes]
    assert "/webhooks/aws-sns" in route_paths

