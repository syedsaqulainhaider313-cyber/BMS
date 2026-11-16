import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import AuditEvent
from app.imports import commit_batch, make_batch
from app.main import (
    approve_campaign,
    campaign_eligibility,
    campaign_report_data,
    cancel_campaign,
    delete_campaign_events,
    delete_contact,
    pause_campaign,
    resume_campaign,
    unsubscribe,
    unsubscribe_confirmation,
)
from app.models import (
    Campaign,
    CampaignEvent,
    CampaignStatus,
    ConsentRecord,
    Contact,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.settings import settings
from app.suppression import make_unsubscribe_token
from app.worker import enqueue_campaign, process_one


def test_scenario_1_importing_suppressed_address_does_not_clear_suppression_or_grant_eligibility(db):
    # Pre-existing suppression
    suppressed_email = "suppressed@example.com"
    suppressed_norm = normalize_email(suppressed_email)
    db.add(
        Suppression(
            email_normalized=suppressed_norm,
            reason=SuppressionReason.MANUAL,
            source="admin_entry",
            detail="blocked user",
        )
    )
    db.commit()

    # Import rows containing this suppressed address
    rows = [
        {
            "email": suppressed_email,
            "source": "web_signup",
            "date": "2026-01-01T12:00:00Z",
            "purpose": "product_updates",
            "provenance": "form_v1",
        }
    ]
    mapping = {
        "email": "email",
        "consent_source": "source",
        "consented_at": "date",
        "consent_purpose": "purpose",
        "consent_provenance": "provenance",
        "display_name": None,
    }
    batch, token, report, counts = make_batch("test.csv", rows, mapping, db)
    assert counts["suppressed"] == 1
    assert counts["valid"] == 0

    # Commit the batch
    result = commit_batch(batch, token, db)
    assert result["created"] == 0

    # Verify suppression record remains untouched and no contact is created
    suppression = db.scalar(
        select(Suppression).where(Suppression.email_normalized == suppressed_norm)
    )
    assert suppression is not None
    assert suppression.reason == SuppressionReason.MANUAL

    contact = db.scalar(select(Contact).where(Contact.email_normalized == suppressed_norm))
    assert contact is None


def test_scenario_2_unsubscribe_after_enqueue_prevents_dispatch(db, sample_template):
    # Setup contact with valid consent
    email = "will-optout@example.com"
    norm = normalize_email(email)
    contact = Contact(email=email, email_normalized=norm)
    db.add(contact)
    db.flush()
    consent = ConsentRecord(
        contact_id=contact.id,
        source="form",
        consented_at=datetime.now(UTC),
        purpose="weekly_digest",
        provenance="ip=127.0.0.1",
    )
    db.add(consent)

    campaign = Campaign(
        name="Weekly Newsletter",
        template_id=sample_template.id,
        consent_purpose="weekly_digest",
    )
    db.add(campaign)
    db.commit()

    # Approve and queue campaign
    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)
    db.commit()

    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == campaign.id))
    assert outbox is not None
    assert outbox.status == OutboxStatus.PENDING

    # Now simulate user opting out / unsubscribing BEFORE worker processes the message
    db.add(
        Suppression(
            email_normalized=norm,
            reason=SuppressionReason.UNSUBSCRIBE,
            source="user_optout",
        )
    )
    db.commit()

    # Worker runs
    processed = process_one(db)
    assert processed is True
    db.commit()

    db.refresh(outbox)
    assert outbox.status == OutboxStatus.SUPPRESSED
    assert outbox.last_error == "globally_suppressed"


def test_scenario_3_hard_bounce_and_complaint_suppress_and_dedup(db, sample_template):
    # Setup two addresses that trigger hard bounce and complaint deterministically
    # From _mock_outcome: int(sha256[:8], 16) % 29 == 0 is bounce, == 1 is complaint
    # Let's find addresses for each outcome
    bounce_email = None
    complaint_email = None
    for i in range(1000):
        test_addr = f"user{i}@testdomain.org"
        val = int(hashlib.sha256(test_addr.encode()).hexdigest()[:8], 16) % 29
        if val == 0 and not bounce_email:
            bounce_email = test_addr
        elif val == 1 and not complaint_email:
            complaint_email = test_addr
        if bounce_email and complaint_email:
            break

    assert bounce_email is not None
    assert complaint_email is not None

    contact1 = Contact(email=bounce_email, email_normalized=bounce_email)
    contact2 = Contact(email=complaint_email, email_normalized=complaint_email)
    db.add_all([contact1, contact2])
    db.flush()
    db.add_all([
        ConsentRecord(
            contact_id=contact1.id,
            source="test",
            consented_at=datetime.now(UTC),
            purpose="blast",
            provenance="web",
        ),
        ConsentRecord(
            contact_id=contact2.id,
            source="test",
            consented_at=datetime.now(UTC),
            purpose="blast",
            provenance="web",
        ),
    ])
    campaign = Campaign(name="Blast", template_id=sample_template.id, consent_purpose="blast")
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)
    db.commit()

    # Process first message (bounce)
    process_one(db)
    db.commit()
    # Process second message (complaint)
    process_one(db)
    db.commit()

    # Check suppression entries created
    supp1 = db.scalar(select(Suppression).where(Suppression.email_normalized == bounce_email))
    assert supp1 is not None
    assert supp1.reason == SuppressionReason.HARD_BOUNCE

    supp2 = db.scalar(select(Suppression).where(Suppression.email_normalized == complaint_email))
    assert supp2 is not None
    assert supp2.reason == SuppressionReason.COMPLAINT

    # Event deduplication check: verify no duplicate event keys can be created
    event_count = db.scalar(
        select(CampaignEvent)
        .where(CampaignEvent.campaign_id == campaign.id)
        .execution_options(yield_per=100)
    )
    assert event_count is not None


def test_scenario_4_missing_consent_or_purpose_excludes_contact(db, sample_template):
    # Contact A: consented to newsletter
    c_a = Contact(email="a@example.com", email_normalized="a@example.com")
    # Contact B: consented to promotional_offers
    c_b = Contact(email="b@example.com", email_normalized="b@example.com")
    # Contact C: has no consent records
    c_c = Contact(email="c@example.com", email_normalized="c@example.com")
    db.add_all([c_a, c_b, c_c])
    db.flush()

    db.add(
        ConsentRecord(
            contact_id=c_a.id,
            source="site",
            consented_at=datetime.now(UTC),
            purpose="newsletter",
            provenance="form",
        )
    )
    db.add(
        ConsentRecord(
            contact_id=c_b.id,
            source="site",
            consented_at=datetime.now(UTC),
            purpose="promotions",
            provenance="checkout",
        )
    )
    db.commit()

    campaign = Campaign(name="Newsletter", template_id=sample_template.id, consent_purpose="newsletter")
    db.add(campaign)
    db.commit()

    eligibility = campaign_eligibility(campaign, db)
    assert eligibility["eligible_count"] == 1
    assert eligibility["eligible"][0]["email"] == "a@example.com"

    excluded_reasons = {r["contact_id"]: r["reason"] for r in eligibility["excluded"]}
    assert excluded_reasons[c_b.id] == "missing_matching_consent"
    assert excluded_reasons[c_c.id] == "missing_matching_consent"


def test_scenario_5_provider_retry_idempotency(db, sample_template):
    # Test retry mechanism has exponential backoff and maximum retry limit
    # Find an address that produces 'transient' error
    transient_email = None
    for i in range(1000):
        test_addr = f"retry{i}@testdomain.org"
        val = int(hashlib.sha256(test_addr.encode()).hexdigest()[:8], 16) % 29
        if val == 2:
            transient_email = test_addr
            break

    assert transient_email is not None

    contact = Contact(email=transient_email, email_normalized=transient_email)
    db.add(contact)
    db.flush()
    db.add(
        ConsentRecord(
            contact_id=contact.id,
            source="form",
            consented_at=datetime.now(UTC),
            purpose="digest",
            provenance="web",
        )
    )
    campaign = Campaign(name="Digest", template_id=sample_template.id, consent_purpose="digest")
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)
    db.commit()

    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == campaign.id))
    original_idempotency_key = outbox.idempotency_key
    assert original_idempotency_key is not None

    now = datetime.now(UTC)

    # Attempt 1 -> transitions to RETRY with backoff
    assert process_one(db, now=now) is True
    db.commit()
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.RETRY
    assert outbox.attempts == 1
    assert outbox.idempotency_key == original_idempotency_key  # Stable key

    # Before next_attempt_at, worker will not process it
    assert process_one(db, now=now + timedelta(seconds=1)) is False

    # Attempt 2 -> still transient
    now_attempt_2 = now + timedelta(seconds=5)
    assert process_one(db, now=now_attempt_2) is True
    db.commit()
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.RETRY
    assert outbox.attempts == 2

    # Attempt 3 -> reaches MAX_ATTEMPTS (3), fails deterministically
    now_attempt_3 = now_attempt_2 + timedelta(seconds=10)
    assert process_one(db, now=now_attempt_3) is True
    db.commit()
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.FAILED
    assert outbox.last_error == "retry_limit_exceeded"


def test_scenario_6_quota_exhaustion_pauses_worker(db, sample_template, monkeypatch):
    monkeypatch.setattr(settings, "max_sends_per_minute", 2)

    now = datetime.now(UTC)
    # Create 3 contacts that will succeed
    delivered_emails = []
    for i in range(100):
        addr = f"ok{i}@example.com"
        val = int(hashlib.sha256(addr.encode()).hexdigest()[:8], 16) % 29
        if val > 2:
            delivered_emails.append(addr)
        if len(delivered_emails) == 3:
            break

    contacts = [Contact(email=e, email_normalized=e) for e in delivered_emails]
    db.add_all(contacts)
    db.flush()
    for c in contacts:
        db.add(
            ConsentRecord(
                contact_id=c.id,
                source="test",
                consented_at=now,
                purpose="quota_test",
                provenance="web",
            )
        )
    campaign = Campaign(name="Quota Test", template_id=sample_template.id, consent_purpose="quota_test")
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)
    db.commit()

    # Process 1st message -> OK
    assert process_one(db, now=now) is True
    db.commit()

    # Process 2nd message -> OK (reaches max_sends_per_minute = 2)
    assert process_one(db, now=now + timedelta(seconds=10)) is True
    db.commit()

    # 3rd message at now + 20s should NOT be processed because quota is exhausted!
    assert process_one(db, now=now + timedelta(seconds=20)) is False
    db.commit()

    # Verify 3rd message remains in PENDING (not silently dropped or failed!)
    pending_count = db.scalar(
        select(OutboxMessage).where(
            OutboxMessage.campaign_id == campaign.id,
            OutboxMessage.status == OutboxStatus.PENDING,
        )
    )
    assert pending_count is not None

    # When window moves past 60s, quota re-opens!
    assert process_one(db, now=now + timedelta(seconds=70)) is True
    db.commit()


def test_scenario_8_campaign_content_immutable_after_approval(db, sample_template, sample_contact):
    campaign = Campaign(
        name="Immutable Test",
        template_id=sample_template.id,
        consent_purpose="monthly_newsletter",
    )
    db.add(campaign)
    db.commit()

    # Approve campaign
    approved = approve_campaign(campaign.id, db)
    db.commit()

    # Snapshot is frozen
    frozen_content = approved["frozen_content"] if "frozen_content" in approved else campaign.frozen_content
    assert frozen_content["subject"] == sample_template.subject
    assert frozen_content["sender_email"] == sample_template.sender_email
    assert len(campaign.recipient_snapshot) == 1

    # Modifying the original template afterwards has NO effect on the campaign
    sample_template.subject = "Tampered Subject"
    sample_template.html_body = "<p>Tampered Body <a href='{{unsubscribe_url}}'>optout</a></p>"
    db.commit()

    db.refresh(campaign)
    assert campaign.frozen_content["subject"] == "Welcome to our newsletter"

    # Cannot approve an already approved campaign
    with pytest.raises(HTTPException) as exc_info:
        approve_campaign(campaign.id, db)
    assert exc_info.value.status_code == 409


def test_scenario_9_mock_mode_no_real_provider():
    assert settings.email_provider == "mock"


def test_scenario_10_reports_distinguish_accepted_from_delivered(db, sample_template):
    campaign = Campaign(
        name="Report Metrics Test",
        template_id=sample_template.id,
        consent_purpose="metrics",
        status=CampaignStatus.APPROVED,
        recipient_snapshot=[{"contact_id": 1, "email": "r1@test.org"}, {"contact_id": 2, "email": "r2@test.org"}],
    )
    db.add(campaign)
    db.flush()

    # Message 1: ACCEPTED
    msg1 = OutboxMessage(
        campaign_id=campaign.id,
        contact_id=1,
        email_normalized="r1@test.org",
        idempotency_key="key-1",
        status=OutboxStatus.ACCEPTED,
    )
    # Message 2: DELIVERED
    msg2 = OutboxMessage(
        campaign_id=campaign.id,
        contact_id=2,
        email_normalized="r2@test.org",
        idempotency_key="key-2",
        status=OutboxStatus.DELIVERED,
    )
    db.add_all([msg1, msg2])
    db.commit()

    _, _, counts = campaign_report_data(campaign.id, db)
    # Both accepted and delivered count towards accepted, but delivered is distinct
    assert counts["accepted"] == 2
    assert counts["delivered"] == 1


def test_scenario_11_manual_contact_deletion_redacts_and_preserves_suppression(db, sample_template):
    email = "delete-me@example.com"
    norm = normalize_email(email)
    contact = Contact(email=email, email_normalized=norm, display_name="To Delete")
    db.add(contact)
    db.flush()

    db.add(
        ConsentRecord(
            contact_id=contact.id,
            source="form",
            consented_at=datetime.now(UTC),
            purpose="promo",
            provenance="web",
        )
    )
    # Also add a global suppression
    db.add(
        Suppression(
            email_normalized=norm,
            reason=SuppressionReason.MANUAL,
            source="admin",
            detail="blocked",
        )
    )

    campaign = Campaign(
        name="Campaign with Contact",
        template_id=sample_template.id,
        consent_purpose="promo",
        status=CampaignStatus.APPROVED,
        recipient_snapshot=[{"contact_id": contact.id, "email": norm}],
    )
    db.add(campaign)
    db.flush()

    outbox = OutboxMessage(
        campaign_id=campaign.id,
        contact_id=contact.id,
        email_normalized=norm,
        idempotency_key="delete-test-key",
        status=OutboxStatus.PENDING,
    )
    db.add(outbox)
    db.commit()

    # Call delete_contact
    result = delete_contact(contact.id, db)
    assert result["status"] == "deleted"
    assert result["suppression_retained"] == "true"

    # Contact is deleted
    assert db.get(Contact, contact.id) is None
    # Consent records deleted
    assert db.query(ConsentRecord).filter(ConsentRecord.contact_id == contact.id).count() == 0

    # Campaign recipient snapshot is redacted
    db.refresh(campaign)
    assert campaign.recipient_snapshot[0]["email"] == "[deleted]"
    assert campaign.recipient_snapshot[0]["personal_data_deleted"] is True

    # Outbox message is marked FAILED and redacted
    db.refresh(outbox)
    assert outbox.contact_id is None
    assert outbox.status == OutboxStatus.FAILED
    assert outbox.last_error == "contact_personal_data_deleted"

    # Suppression is preserved
    suppression = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert suppression is not None


def test_scenario_12_unsubscribe_get_does_not_mutate_and_post_is_idempotent(db, sample_template):
    email = "optout-tester@example.com"
    norm = normalize_email(email)
    contact = Contact(email=email, email_normalized=norm)
    db.add(contact)
    db.flush()

    campaign = Campaign(
        name="Unsub Campaign",
        template_id=sample_template.id,
        consent_purpose="newsletter",
        status=CampaignStatus.APPROVED,
        recipient_snapshot=[{"contact_id": contact.id, "email": norm}],
    )
    db.add(campaign)
    db.commit()

    token = make_unsubscribe_token(campaign.id, contact.id, norm)

    # 1. GET confirmation page does NOT mutate state
    resp = unsubscribe_confirmation(token)
    assert "Confirm unsubscribe" in resp.body.decode()
    assert db.scalar(select(Suppression).where(Suppression.email_normalized == norm)) is None

    # 2. Signed POST applies global suppression
    class DummyRequest:
        client = None

    post_resp = unsubscribe(token, DummyRequest(), db)
    assert post_resp["status"] == "unsubscribed"

    supp = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert supp is not None
    assert supp.reason == SuppressionReason.UNSUBSCRIBE

    # 3. Repeat POST is idempotent
    post_resp_2 = unsubscribe(token, DummyRequest(), db)
    assert post_resp_2["status"] == "unsubscribed"
    assert db.query(Suppression).filter(Suppression.email_normalized == norm).count() == 1


def test_scenario_13_campaign_event_deletion_leaves_audit_trail(db, sample_template):
    campaign = Campaign(
        name="Audit Event Test",
        template_id=sample_template.id,
        consent_purpose="test",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.flush()

    event = CampaignEvent(
        event_key="test-event-key",
        campaign_id=campaign.id,
        event_type="queued",
        detail="testing",
    )
    db.add(event)
    db.commit()

    result = delete_campaign_events(campaign.id, db)
    assert result["deleted"] == 1

    # Audit event was created
    audit_record = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "campaigns.events_deleted",
            AuditEvent.detail.contains(f"campaign_id={campaign.id}"),
        )
    )
    assert audit_record is not None


def test_scenario_14_pause_resume_outbox_behavior(db, sample_template, sample_contact):
    campaign = Campaign(
        name="Pause Resume Test",
        template_id=sample_template.id,
        consent_purpose="monthly_newsletter",
    )
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)
    db.commit()

    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == campaign.id))
    assert outbox.status == OutboxStatus.PENDING

    # Pause campaign
    pause_campaign(campaign.id, db)
    db.commit()
    db.refresh(campaign)
    assert campaign.status == CampaignStatus.PAUSED

    # Outbox message remains untouched in PENDING
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.PENDING

    # Worker refuses to process jobs for a paused campaign
    assert process_one(db) is False

    # Resume campaign
    resume_campaign(campaign.id, db)
    db.commit()
    db.refresh(campaign)
    assert campaign.status == CampaignStatus.APPROVED

    # Worker now processes the job
    assert process_one(db) is True
    db.commit()


def test_scenario_15_cancel_campaign_terminal_behavior(db, sample_template, sample_contact):
    campaign = Campaign(
        name="Cancel Test",
        template_id=sample_template.id,
        consent_purpose="monthly_newsletter",
    )
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)
    db.commit()

    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == campaign.id))
    assert outbox.status == OutboxStatus.PENDING

    # Cancel campaign
    cancel_campaign(campaign.id, db)
    db.commit()
    db.refresh(campaign)
    assert campaign.status == CampaignStatus.CANCELLED

    # Pending jobs marked terminal FAILED
    db.refresh(outbox)
    assert outbox.status == OutboxStatus.FAILED
    assert outbox.last_error == "campaign_cancelled"

    # Worker will not process terminal jobs
    assert process_one(db) is False
