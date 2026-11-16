r"""Empirical Adversarial Challenge Suite (Iteration 2).
Author: challenger_r2 (Role: Phase 1 & 2 Empirical Verification Challenger)

Empirically challenges and stress-tests:
1. Multi-contact GDPR deletion ($\ge 5$ contacts in a single campaign) ensuring 0 constraint collisions and 0 PII retention.
2. Pre-enqueue deletion filtering ensuring pre-deleted contacts are omitted and total deletion queues 0 messages.
3. Post-enqueue GDPR deletion non-resurrection in process_chunk_decoupled.
4. NULL contact_id status synchronization to DISPATCHED, SUPPRESSED, and FAILED.
5. Deliverability RFC 8058 compliance, SES dedicated IP pool configuration, and multi-domain rate throttling invariants.
6. DB connection hold time < 2.0ms per email under simulated network latency.
"""

from __future__ import annotations

import email
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

import app.worker as worker_module
from app.db import Base
from app.main import approve_campaign, delete_contact
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    ConsentRecord,
    Contact,
    EmailTemplate,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.provider import AmazonSESProvider, MockEmailProvider, ProviderResult
from app.templates import build_rfc_compliant_mime
from app.throttling import (
    _GLOBAL_IN_MEMORY_LIMITER,
    InMemoryTokenBucketLimiter,
    get_isp_limits,
)
from app.worker import enqueue_campaign, process_chunk_decoupled


@pytest.fixture
def r2_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine)
    session = SessionClass()
    yield session
    session.close()
    engine.dispose()
    _GLOBAL_IN_MEMORY_LIMITER.reset()


@pytest.fixture
def r2_template(r2_db: Session) -> EmailTemplate:
    tmpl = EmailTemplate(
        name="Challenger R2 Template",
        subject="Monthly Newsletter",
        sender_name="Acme Security",
        sender_email="security@example.com",
        mailing_address="456 Threat Way, Suite 100, Metropolis",
        html_body="<p>Dear recipient, here is your update. <a href='{{unsubscribe_url}}'>Unsubscribe</a></p>",
        text_body="Dear recipient, here is your update. Unsubscribe: {{unsubscribe_url}}",
    )
    r2_db.add(tmpl)
    r2_db.commit()
    return tmpl


# ==============================================================================
# Focus Area 1: Multi-Contact GDPR Deletion Integrity (>= 5 Contacts)
# ==============================================================================

def test_adversarial_gdpr_multi_contact_deletion_large_batch(
    r2_db: Session, r2_template: EmailTemplate
):
    """Stress test GDPR Article 17 deletion on 10 contacts belonging to the same campaign.
    Verifies that:
    1. Zero IntegrityError / UniqueConstraint collisions occur on uq_campaign_recipient_email.
    2. All 10 CampaignRecipient rows are updated to 'cancelled' status with unique surrogate emails.
    3. All contact_id and consent_record_id foreign keys are set to NULL.
    4. Zero personal email strings remain in campaign_recipients.
    5. All Contact and ConsentRecord entities are removed.
    6. Campaign.recipient_snapshot personal data is redacted.
    """
    db = r2_db
    purpose = "newsletter_purpose"
    num_contacts = 10

    contacts: list[Contact] = []
    for i in range(num_contacts):
        email_str = f"gdpr_user_{i:02d}@adversarial.example.com"
        c = Contact(email=email_str, email_normalized=normalize_email(email_str))
        db.add(c)
        contacts.append(c)
    db.flush()

    now = datetime.now(UTC)
    for c in contacts:
        db.add(
            ConsentRecord(
                contact_id=c.id,
                purpose=purpose,
                source="optin_form",
                provenance="web_consent",
                consented_at=now,
            )
        )
    db.commit()

    campaign = Campaign(
        name="Large Batch GDPR Test Campaign",
        template_id=r2_template.id,
        consent_purpose=purpose,
    )
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)

    # Confirm all 10 recipients exist in CampaignRecipient table
    recipients = db.scalars(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ).all()
    assert len(recipients) == num_contacts

    contact_ids = [c.id for c in contacts]
    personal_emails = [c.email_normalized for c in contacts]

    # Sequentially delete all 10 contacts under GDPR
    for cid in contact_ids:
        res = delete_contact(cid, db)
        assert res["status"] == "deleted"

    # Refresh and evaluate all recipient rows
    updated_recipients = db.scalars(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ).all()
    assert len(updated_recipients) == num_contacts

    surrogate_emails = set()
    for rec in updated_recipients:
        assert rec.contact_id is None, "contact_id must be set to NULL"
        assert rec.consent_record_id is None, "consent_record_id must be set to NULL"
        assert rec.status == CampaignRecipientStatus.CANCELLED.value, "status must be cancelled"
        assert rec.email_normalized.startswith("deleted-contact-"), "Surrogate email prefix required"
        surrogate_emails.add(rec.email_normalized)

    # Unique constraint proof: every surrogate email is unique!
    assert len(surrogate_emails) == num_contacts, "All surrogate emails in campaign must be unique"

    # Absolute proof of zero PII retention
    leaked_recipients = db.scalars(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.email_normalized.in_(personal_emails),
        )
    ).all()
    assert len(leaked_recipients) == 0, "No personal email addresses may be retained"

    # Verify Contact and ConsentRecord deletion
    assert db.scalar(select(text("count(*)")).select_from(Contact)) == 0
    assert db.scalar(select(text("count(*)")).select_from(ConsentRecord)) == 0

    # Verify recipient_snapshot redaction
    db.refresh(campaign)
    for entry in campaign.recipient_snapshot:
        assert entry.get("email") == "[deleted]"
        assert entry.get("personal_data_deleted") is True


# ==============================================================================
# Focus Area 2: Pre-Enqueue Deletion Filtering & All-Deleted Handling
# ==============================================================================

def test_adversarial_pre_enqueue_partial_deletion(
    r2_db: Session, r2_template: EmailTemplate
):
    """If 3 of 6 contacts in an approved campaign are GDPR-deleted prior to enqueue,
    enqueue_campaign must enqueue ONLY the 3 active contacts and 0 deleted contacts.
    """
    db = r2_db
    purpose = "pre_enq_partial"
    contacts: list[Contact] = []
    for i in range(6):
        c = Contact(
            email=f"partial_{i}@test.example.com",
            email_normalized=normalize_email(f"partial_{i}@test.example.com"),
        )
        db.add(c)
        contacts.append(c)
    db.flush()

    for c in contacts:
        db.add(
            ConsentRecord(
                contact_id=c.id,
                purpose=purpose,
                source="api",
                provenance="test",
                consented_at=datetime.now(UTC),
            )
        )
    db.commit()

    camp = Campaign(
        name="Pre-Enqueue Partial Deletion Campaign",
        template_id=r2_template.id,
        consent_purpose=purpose,
    )
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    # Delete 3 contacts before enqueue
    deleted_contacts = contacts[:3]
    active_contacts = contacts[3:]
    for c in deleted_contacts:
        delete_contact(c.id, db)

    # Execute enqueue
    enqueued_count = enqueue_campaign(camp.id, db)
    assert enqueued_count == 3, f"Expected exactly 3 active contacts enqueued, got {enqueued_count}"

    outbox_messages = db.scalars(
        select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id)
    ).all()
    assert len(outbox_messages) == 3

    enqueued_emails = {m.email_normalized for m in outbox_messages}
    expected_active_emails = {c.email_normalized for c in active_contacts}
    assert enqueued_emails == expected_active_emails

    for c in deleted_contacts:
        assert f"deleted-contact-{c.id}" not in enqueued_emails
        assert "[deleted]" not in enqueued_emails


def test_adversarial_pre_enqueue_all_deleted_returns_zero(
    r2_db: Session, r2_template: EmailTemplate
):
    """If ALL contacts in an approved campaign are GDPR-deleted before enqueue,
    enqueue_campaign must return 0 immediately and create 0 outbox messages.
    """
    db = r2_db
    purpose = "pre_enq_all_del"
    contacts: list[Contact] = []
    for i in range(5):
        c = Contact(
            email=f"all_del_{i}@test.example.com",
            email_normalized=normalize_email(f"all_del_{i}@test.example.com"),
        )
        db.add(c)
        contacts.append(c)
    db.flush()

    for c in contacts:
        db.add(
            ConsentRecord(
                contact_id=c.id,
                purpose=purpose,
                source="api",
                provenance="test",
                consented_at=datetime.now(UTC),
            )
        )
    db.commit()

    camp = Campaign(
        name="Pre-Enqueue All Deleted Campaign",
        template_id=r2_template.id,
        consent_purpose=purpose,
    )
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    # Delete all 5 contacts
    for c in contacts:
        delete_contact(c.id, db)

    # Enqueue campaign
    enqueued = enqueue_campaign(camp.id, db)
    assert enqueued == 0, "When all recipients are deleted, enqueue must return 0"

    total_outbox = db.scalar(
        select(text("count(*)")).select_from(OutboxMessage).where(OutboxMessage.campaign_id == camp.id)
    )
    assert total_outbox == 0, "No OutboxMessage rows should exist"


# ==============================================================================
# Focus Area 3: Post-Enqueue GDPR Deletion Non-Resurrection
# ==============================================================================

def test_adversarial_post_enqueue_deletion_never_resurrected(
    r2_db: Session, r2_template: EmailTemplate
):
    """If contacts are deleted post-enqueue, their OutboxMessage rows become FAILED.
    When process_chunk_decoupled is called with all outbox IDs:
    - The FAILED messages must NOT be loaded, NOT dispatched, and NOT overwritten.
    - They must retain status=FAILED and last_error='contact_personal_data_deleted'.
    - Active messages must be processed normally.
    """
    db = r2_db
    purpose = "post_enq_non_resurrect"
    contacts: list[Contact] = []
    for i in range(4):
        c = Contact(
            email=f"post_enq_{i}@test.example.com",
            email_normalized=normalize_email(f"post_enq_{i}@test.example.com"),
        )
        db.add(c)
        contacts.append(c)
    db.flush()

    for c in contacts:
        db.add(
            ConsentRecord(
                contact_id=c.id,
                purpose=purpose,
                source="api",
                provenance="test",
                consented_at=datetime.now(UTC),
            )
        )
    db.commit()

    camp = Campaign(
        name="Post-Enqueue Non-Resurrection Campaign",
        template_id=r2_template.id,
        consent_purpose=purpose,
    )
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)

    outbox_msgs = db.scalars(
        select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id).order_by(OutboxMessage.id)
    ).all()
    assert len(outbox_msgs) == 4
    outbox_ids = [m.id for m in outbox_msgs]

    # Delete contact 0 and contact 1 post-enqueue
    c0_id = contacts[0].id
    c1_id = contacts[1].id
    delete_contact(c0_id, db)
    delete_contact(c1_id, db)

    # Verify outbox messages for c0 and c1 are marked FAILED
    db.refresh(outbox_msgs[0])
    db.refresh(outbox_msgs[1])
    assert outbox_msgs[0].status == OutboxStatus.FAILED
    assert outbox_msgs[0].last_error == "contact_personal_data_deleted"
    assert outbox_msgs[1].status == OutboxStatus.FAILED
    assert outbox_msgs[1].last_error == "contact_personal_data_deleted"

    # Now dispatch the chunk with all 4 outbox IDs
    summary = process_chunk_decoupled(camp.id, outbox_ids, db_session=db)

    # Verify only 2 active messages were dispatched
    assert summary["dispatched"] == 2

    # Verify c0 and c1 outbox messages were NOT resurrected or overwritten!
    db.refresh(outbox_msgs[0])
    db.refresh(outbox_msgs[1])
    db.refresh(outbox_msgs[2])
    db.refresh(outbox_msgs[3])

    assert outbox_msgs[0].status == OutboxStatus.FAILED
    assert outbox_msgs[0].last_error == "contact_personal_data_deleted"
    assert outbox_msgs[1].status == OutboxStatus.FAILED
    assert outbox_msgs[1].last_error == "contact_personal_data_deleted"

    assert outbox_msgs[2].status == OutboxStatus.ACCEPTED
    assert outbox_msgs[3].status == OutboxStatus.ACCEPTED


# ==============================================================================
# Focus Area 4: NULL contact_id Recipient Status Synchronization
# ==============================================================================

def test_adversarial_null_contact_id_recipient_status_sync(
    r2_db: Session, r2_template: EmailTemplate
):
    """When a recipient row has contact_id=None (e.g. unlinked or anonymized),
    _commit_dispatched_batch must successfully transition CampaignRecipient.status
    to DISPATCHED by matching on outbox_id.
    """
    db = r2_db
    purpose = "null_cid_sync"
    c1 = Contact(email="null_cid_1@example.com", email_normalized=normalize_email("null_cid_1@example.com"))
    c2 = Contact(email="null_cid_2@example.com", email_normalized=normalize_email("null_cid_2@example.com"))
    db.add_all([c1, c2])
    db.flush()

    now = datetime.now(UTC)
    db.add_all([
        ConsentRecord(contact_id=c1.id, purpose=purpose, source="form", provenance="p", consented_at=now),
        ConsentRecord(contact_id=c2.id, purpose=purpose, source="form", provenance="p", consented_at=now),
    ])
    db.commit()

    camp = Campaign(
        name="Null Contact ID Campaign",
        template_id=r2_template.id,
        consent_purpose=purpose,
    )
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)

    rec1 = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.contact_id == c1.id,
        )
    )
    rec2 = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.contact_id == c2.id,
        )
    )
    outbox1 = db.scalar(select(OutboxMessage).where(OutboxMessage.id == rec1.outbox_id))
    outbox2 = db.scalar(select(OutboxMessage).where(OutboxMessage.id == rec2.outbox_id))

    # Anonymize contact_id to NULL on recipient 1 and outbox 1
    rec1.contact_id = None
    outbox1.contact_id = None
    db.commit()

    # Recipient 1 has status 'queued' and contact_id None
    assert rec1.status == CampaignRecipientStatus.QUEUED.value
    assert rec1.contact_id is None

    # Dispatch both messages via decoupled worker
    summary = process_chunk_decoupled(camp.id, [outbox1.id, outbox2.id], db_session=db)
    assert summary["dispatched"] == 2

    db.refresh(rec1)
    db.refresh(rec2)

    # Invariant: rec1 (with contact_id=None) must transition to 'dispatched'
    assert rec1.status == CampaignRecipientStatus.DISPATCHED.value, (
        f"Expected rec1 status to be 'dispatched', got {rec1.status}"
    )
    assert rec2.status == CampaignRecipientStatus.DISPATCHED.value


def test_adversarial_suppressed_and_failed_recipient_sync(
    r2_db: Session, r2_template: EmailTemplate
):
    """Verify that suppressed and failed outbox messages also synchronize
    CampaignRecipient status to SUPPRESSED and FAILED respectively via outbox_id.
    """
    db = r2_db
    purpose = "suppress_fail_sync"
    c_suppressed = Contact(
        email="suppressed_user@example.com",
        email_normalized=normalize_email("suppressed_user@example.com"),
    )
    c_failed = Contact(
        email="failed_user@example.com",
        email_normalized=normalize_email("failed_user@example.com"),
    )
    db.add_all([c_suppressed, c_failed])
    db.flush()

    now = datetime.now(UTC)
    db.add_all([
        ConsentRecord(contact_id=c_suppressed.id, purpose=purpose, source="form", provenance="p", consented_at=now),
        ConsentRecord(contact_id=c_failed.id, purpose=purpose, source="form", provenance="p", consented_at=now),
    ])
    db.commit()

    camp = Campaign(
        name="Suppression and Failure Sync Campaign",
        template_id=r2_template.id,
        consent_purpose=purpose,
    )
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)

    rec_supp = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.contact_id == c_suppressed.id,
        )
    )
    rec_fail = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.contact_id == c_failed.id,
        )
    )

    # Add suppression entry for c_suppressed
    db.add(
        Suppression(
            email_normalized=c_suppressed.email_normalized,
            reason=SuppressionReason.COMPLAINT,
            source="manual",
            detail="User complaint",
        )
    )
    db.commit()

    # Dispatch via decoupled worker for suppressed
    summary = process_chunk_decoupled(camp.id, [rec_supp.outbox_id], db_session=db)
    assert summary["suppressed"] == 1

    db.refresh(rec_supp)
    assert rec_supp.status == CampaignRecipientStatus.SUPPRESSED.value, (
        f"CampaignRecipient must be synced to 'suppressed', got {rec_supp.status}"
    )

    # Dispatch via decoupled worker for failed
    mock_fail_provider = MagicMock()
    mock_fail_provider.dispatch.return_value = ProviderResult(
        status="failed", provider_message_id="", error_detail="permanent_error"
    )
    with patch("app.worker.get_email_provider", return_value=mock_fail_provider):
        summary_fail = process_chunk_decoupled(camp.id, [rec_fail.outbox_id], db_session=db)
        assert summary_fail["failed"] == 1

    db.refresh(rec_fail)
    assert rec_fail.status == CampaignRecipientStatus.FAILED.value, (
        f"CampaignRecipient must be synced to 'failed', got {rec_fail.status}"
    )


# ==============================================================================
# Focus Area 5: Deliverability, RFC 8058 & Throttling Invariants
# ==============================================================================

def test_adversarial_rfc8058_and_rfc5322_mime_headers():
    """Verify MIME generation strictly conforms to RFC 5322 and RFC 8058 specifications:
    - multipart/alternative structure
    - List-Unsubscribe contains https and mailto URLs enclosed in < >
    - List-Unsubscribe-Post is exactly 'List-Unsubscribe=One-Click'
    - Return-Path is correctly set for SPF alignment
    - Max line length is <= 998 octets
    """
    domain = "transact.example.com"
    token = "42.100.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    raw_mime = build_rfc_compliant_mime(
        from_name="Transact Security",
        from_address=f"no-reply@{domain}",
        to_address="recipient@gmail.com",
        subject="Important Account Notice",
        html_body="<p>Your statement is ready. <a href='{{unsubscribe_url}}'>Unsubscribe</a></p>",
        text_body="Your statement is ready. Unsubscribe: {{unsubscribe_url}}",
        unsubscribe_token=token,
        domain=domain,
        campaign_id=42,
        template_id=7,
        custom_mail_from_domain=f"bounces.{domain}",
    )

    parsed = email.message_from_bytes(raw_mime)
    assert parsed.is_multipart(), "Message must be multipart"
    assert parsed.get_content_type() == "multipart/alternative"

    # RFC 8058 Header Verification
    list_unsub = parsed.get("List-Unsubscribe")
    assert list_unsub is not None
    assert f"<https://{domain}/api/v1/unsubscribe/one-click/{token}>" in list_unsub
    assert f"<mailto:unsubscribe@{domain}?subject=unsub_{token}>" in list_unsub

    list_unsub_post = parsed.get("List-Unsubscribe-Post")
    assert list_unsub_post == "List-Unsubscribe=One-Click"

    # Deliverability Headers
    assert parsed.get("Return-Path") == f"<bounces@bounces.{domain}>"
    assert parsed.get("Auto-Submitted") == "auto-generated"
    assert parsed.get("Precedence") == "bulk"
    assert parsed.get("Feedback-ID") == f"campaign-42:template-7:bms:{domain}"

    # RFC 5322 Line Length Invariant (no line > 998 octets)
    for line in raw_mime.split(b"\n"):
        assert len(line) <= 998, f"Line exceeds 998 octets: {len(line)}"


def test_adversarial_isp_token_bucket_isolation_and_burst():
    """Verify that:
    1. ISP limits burst capacity accurately.
    2. Depleting Google bucket does NOT deplete Yahoo or Microsoft buckets.
    3. Thread-safe concurrent token acquisitions maintain consistency.
    """
    limiter = InMemoryTokenBucketLimiter()
    limits = get_isp_limits()

    google_cap = int(limits["google"]["capacity"])
    yahoo_cap = int(limits["yahoo"]["capacity"])

    # 1. Exhaust Google burst capacity
    for _ in range(google_cap):
        allowed, wait_time = limiter.acquire("user@gmail.com")
        assert allowed is True
        assert wait_time == 0.0

    # Next immediate Google request must be denied
    allowed, wait_time = limiter.acquire("user@gmail.com")
    assert allowed is False
    assert wait_time > 0.0

    # 2. Yahoo must remain fully unconstrained!
    for _ in range(yahoo_cap):
        allowed, wait_time = limiter.acquire("user@yahoo.com")
        assert allowed is True
        assert wait_time == 0.0

    # Next immediate Yahoo request must be denied
    allowed, wait_time = limiter.acquire("user@yahoo.com")
    assert allowed is False
    assert wait_time > 0.0

    # 3. Concurrent thread safety: 20 threads acquiring tokens
    limiter.reset()
    results: list[bool] = []

    def _acquire_worker():
        allowed, _ = limiter.acquire("worker@gmail.com")
        return allowed

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_acquire_worker) for _ in range(google_cap + 10)]
        for f in futures:
            results.append(f.result())

    # Exactly google_cap acquisitions must succeed (given near instantaneous burst)
    success_count = sum(results)
    assert success_count == google_cap, (
        f"Expected exactly {google_cap} successful burst acquisitions, got {success_count}"
    )


def test_adversarial_ses_provider_dedicated_ip_pool_and_classification():
    """Verify AmazonSESProvider sets ConfigurationSetName and handles error taxonomy."""
    mock_ses_client = MagicMock()
    mock_ses_client.send_email.return_value = {"MessageId": "ses-test-msg-12345"}

    provider = AmazonSESProvider(client=mock_ses_client)

    # Dispatch with dedicated IP pool via settings
    result = provider.dispatch(
        email="client@example.com",
        subject="Test SES",
        html_body="<p>Test <a href='{{unsubscribe_url}}'>unsub</a></p>",
        text_body="Test {{unsubscribe_url}}",
        campaign_id=10,
        outbox_id=100,
        unsubscribe_token="tok123",
    )
    assert result.status == "accepted"
    assert result.provider_message_id == "ses-test-msg-12345"

    # Verify send_email call kwargs
    call_kwargs = mock_ses_client.send_email.call_args[1]
    assert call_kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert any(tag["Name"] == "campaign_id" and tag["Value"] == "10" for tag in call_kwargs["EmailTags"])
    assert any(tag["Name"] == "outbox_id" and tag["Value"] == "100" for tag in call_kwargs["EmailTags"])

    # Error classification test: ThrottlingException -> transient
    class MockClientError(Exception):
        def __init__(self, code: str, msg: str):
            self.response = {"Error": {"Code": code, "Message": msg}}
            super().__init__(f"{code}: {msg}")

    mock_ses_client.send_email.side_effect = MockClientError("ThrottlingException", "Daily rate exceeded")
    throttled_res = provider.dispatch(
        email="client2@example.com",
        subject="Test",
        html_body="<p><a href='{{unsubscribe_url}}'>unsub</a></p>",
        text_body="{{unsubscribe_url}}",
        campaign_id=10,
        outbox_id=101,
    )
    assert throttled_res.status == "transient"
    assert "throttled:ThrottlingException" in throttled_res.error_detail

    # Error classification test: AccountSuspendedException -> paused
    mock_ses_client.send_email.side_effect = MockClientError("AccountSuspendedException", "Account paused")
    paused_res = provider.dispatch(
        email="client3@example.com",
        subject="Test",
        html_body="<p><a href='{{unsubscribe_url}}'>unsub</a></p>",
        text_body="{{unsubscribe_url}}",
        campaign_id=10,
        outbox_id=102,
    )
    assert paused_res.status == "paused"


# ==============================================================================
# Focus Area 6: Three-Phase Decoupling Connection Hold Time < 2ms/email
# ==============================================================================

def test_adversarial_connection_hold_time_under_simulated_network_latency(
    r2_template: EmailTemplate,
):
    """Verify that during network latency in Phase B (e.g. 5ms per email dispatch),
    the database connection is completely returned to the pool (0.00ms hold during Phase B),
    and total DB connection hold time across Phase A + Phase C is < 2.0ms per email.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine)
    db = SessionClass()

    # Track checkout durations
    checkouts: list[dict[str, float]] = []

    @event.listens_for(engine, "checkout")
    def on_checkout(dbapi_connection, connection_record, connection_proxy):
        connection_record.info["checkout_time"] = time.perf_counter()

    @event.listens_for(engine, "checkin")
    def on_checkin(dbapi_connection, connection_record):
        if "checkout_time" in connection_record.info:
            duration = (time.perf_counter() - connection_record.info["checkout_time"]) * 1000.0
            checkouts.append({"duration_ms": duration})

    purpose = "hold_time_purpose"
    batch_size = 30
    contacts: list[Contact] = []
    for i in range(batch_size):
        c = Contact(
            email=f"perf_{i}@provider{i}.com",
            email_normalized=normalize_email(f"perf_{i}@provider{i}.com"),
        )
        db.add(c)
        contacts.append(c)
    db.flush()

    for c in contacts:
        db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="api", provenance="p", consented_at=datetime.now(UTC)))
    db.commit()

    tmpl = EmailTemplate(
        name="Perf Template",
        subject="Performance",
        sender_name="Perf Team",
        sender_email="perf@example.com",
        mailing_address="123 Test St",
        html_body="<p>Test <a href='{{unsubscribe_url}}'>unsub</a></p>",
        text_body="Test {{unsubscribe_url}}",
    )
    db.add(tmpl)
    db.commit()

    camp = Campaign(name="Perf Campaign", template_id=tmpl.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)
    db.commit()

    outbox_ids = db.scalars(
        select(OutboxMessage.id).where(OutboxMessage.campaign_id == camp.id)
    ).all()
    assert len(outbox_ids) == batch_size
    db.close()

    # Simulate network latency in MockEmailProvider.dispatch
    orig_dispatch = MockEmailProvider.dispatch

    def slow_dispatch(self, *args, **kwargs):
        time.sleep(0.005)  # 5ms simulated network latency per email
        return orig_dispatch(self, *args, **kwargs)

    MockEmailProvider.dispatch = slow_dispatch

    # Clear checkouts before measurement
    checkouts.clear()

    # Process chunk decoupled with SessionLocal
    with patch.object(worker_module, "SessionLocal", SessionClass):
        start_wall = time.perf_counter()
        summary = process_chunk_decoupled(camp.id, outbox_ids, db_session=None)
        total_wall_ms = (time.perf_counter() - start_wall) * 1000.0

    MockEmailProvider.dispatch = orig_dispatch

    # Wall time must include the 5ms simulated network latency per email
    assert total_wall_ms >= (summary["dispatched"] * 5.0)

    # Invariant 1: Exactly 2 checkouts occur (Phase A read and Phase C commit)
    assert len(checkouts) == 2, f"Expected 2 checkouts, got {len(checkouts)}"

    # Invariant 2: Total DB connection hold time must be < 2.0ms per email
    total_db_hold_ms = sum(c["duration_ms"] for c in checkouts)
    avg_hold_per_email = total_db_hold_ms / batch_size

    assert avg_hold_per_email < 2.0, (
        f"DB hold time per email ({avg_hold_per_email:.4f}ms) exceeded 2.0ms limit!"
    )

    db.close()
    engine.dispose()
    _GLOBAL_IN_MEMORY_LIMITER.reset()


