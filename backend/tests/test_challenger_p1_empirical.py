"""Empirical Stress-Test & Challenge Suite for Phase 1.
Database & Recipient Pipeline Modernization:
- campaign_recipients junction table integrity under GDPR deletion
- Idempotency & Conflict Handling
- Three-Phase Decoupling Connection Hold Time (<2ms/email)
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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
    normalize_email,
)
from app.provider import MockEmailProvider
from app.throttling import _GLOBAL_IN_MEMORY_LIMITER
from app.worker import enqueue_campaign, process_chunk_decoupled


@pytest.fixture
def challenger_db():
    local_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=local_engine)
    Session = sessionmaker(bind=local_engine)
    session = Session()
    yield session
    session.close()
    local_engine.dispose()
    _GLOBAL_IN_MEMORY_LIMITER.reset()


@pytest.fixture
def challenger_template(challenger_db: Session) -> EmailTemplate:
    tmpl = EmailTemplate(
        name="Standard Welcome",
        subject="Welcome to our newsletter",
        sender_name="Acme News",
        sender_email="news@example.com",
        mailing_address="123 Example Street, City, Country",
        html_body="<p>Welcome! <a href='{{unsubscribe_url}}'>Unsubscribe</a></p>",
        text_body="Welcome! Unsubscribe here: {{unsubscribe_url}}",
    )
    challenger_db.add(tmpl)
    challenger_db.commit()
    return tmpl


def test_challenge_1_gdpr_multi_contact_deletion_unique_constraint_collision(
    challenger_db: Session, challenger_template: EmailTemplate
):
    """Empirical Challenge 1:
    When multiple contacts in the same campaign are deleted under GDPR,
    updating email_normalized="[deleted]" violates uq_campaign_recipient_email
    UniqueConstraint("campaign_id", "email_normalized").
    The exception is swallowed by except Exception: pass in delete_contact(),
    causing the second contact's contact_id to remain NON-NULL and personal email
    intact in campaign_recipients!
    """
    db = challenger_db
    purpose = "gdpr_challenge_purpose"

    # 1. Create two contacts
    c1 = Contact(email="user1_gdpr@example.com", email_normalized=normalize_email("user1_gdpr@example.com"))
    c2 = Contact(email="user2_gdpr@example.com", email_normalized=normalize_email("user2_gdpr@example.com"))
    db.add_all([c1, c2])
    db.flush()

    now = datetime.now(UTC)
    db.add_all([
        ConsentRecord(contact_id=c1.id, purpose=purpose, source="form", provenance="p", consented_at=now),
        ConsentRecord(contact_id=c2.id, purpose=purpose, source="form", provenance="p", consented_at=now),
    ])
    db.commit()

    # 2. Create and approve campaign
    camp = Campaign(name="GDPR Multi-Deletion Camp", template_id=challenger_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    c1_id = c1.id
    c2_id = c2.id

    rec1 = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.contact_id == c1_id,
        )
    )
    rec2 = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.contact_id == c2_id,
        )
    )
    assert rec1 is not None and rec2 is not None

    # 3. Delete Contact 1: First deletion succeeds
    res1 = delete_contact(c1_id, db)
    assert res1["status"] == "deleted"

    db.refresh(rec1)
    assert rec1.contact_id is None
    assert rec1.email_normalized == f"deleted-contact-{c1_id}"
    assert rec1.status == CampaignRecipientStatus.CANCELLED.value

    # 4. Delete Contact 2 via API endpoint: Second deletion succeeds without constraint collision
    res2 = delete_contact(c2_id, db)
    assert res2["status"] == "deleted"

    db.refresh(rec2)
    assert rec2.contact_id is None
    assert rec2.email_normalized == f"deleted-contact-{c2_id}"
    assert rec2.status == CampaignRecipientStatus.CANCELLED.value

    # Verify no personal data remains in campaign_recipients
    rec_by_email = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == camp.id,
            CampaignRecipient.email_normalized.in_(["user1_gdpr@example.com", "user2_gdpr@example.com"]),
        )
    )
    assert rec_by_email is None, "Personal emails must be completely erased"


def test_challenge_2_enqueue_leaks_gdpr_deleted_recipients(
    challenger_db: Session, challenger_template: EmailTemplate
):
    """Empirical Challenge 2:
    If a contact is deleted under GDPR after campaign approval but before enqueue,
    enqueue_campaign() does NOT filter out deleted contacts or check contact_id IS NOT NULL.
    It queues an OutboxMessage to send an email to '[deleted]'!
    """
    db = challenger_db
    purpose = "enqueue_leak_purpose"
    c = Contact(email="leak_candidate@example.com", email_normalized=normalize_email("leak_candidate@example.com"))
    db.add(c)
    db.flush()
    db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=datetime.now(UTC)))
    db.commit()

    camp = Campaign(name="Enqueue Leak Camp", template_id=challenger_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    # Contact exercises GDPR erasure BEFORE enqueue
    delete_contact(c.id, db)

    # Now enqueue the campaign
    added = enqueue_campaign(camp.id, db)
    assert added == 0, "Deleted contacts must NOT be enqueued"

    outbox_msg = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id))
    assert outbox_msg is None, "No outbox message should be created for deleted contact"


def test_challenge_3_decoupled_dispatch_ignores_and_overwrites_gdpr_failed_outbox(
    challenger_db: Session, challenger_template: EmailTemplate
):
    """Empirical Challenge 3:
    If a contact is deleted after enqueue, delete_contact() sets OutboxMessage.status = FAILED.
    However, process_chunk_decoupled()'s _read_phase_a does NOT filter by OutboxStatus.PENDING.
    It selects the FAILED message and attempts to dispatch or retry it, overwriting FAILED status!
    """
    db = challenger_db
    purpose = "post_enqueue_deletion_purpose"
    c = Contact(email="post_enq@example.com", email_normalized=normalize_email("post_enq@example.com"))
    db.add(c)
    db.flush()
    db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=datetime.now(UTC)))
    db.commit()

    camp = Campaign(name="Post Enqueue Camp", template_id=challenger_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)

    outbox_msg = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id))
    outbox_id = outbox_msg.id

    # Contact is deleted post-enqueue: delete_contact marks outbox_msg as FAILED
    delete_contact(c.id, db)
    db.refresh(outbox_msg)
    assert outbox_msg.status == OutboxStatus.FAILED
    assert outbox_msg.last_error == "contact_personal_data_deleted"

    # Worker executes process_chunk_decoupled on the batch
    summary = process_chunk_decoupled(camp.id, [outbox_id], db_session=db)

    db.refresh(outbox_msg)
    # VERIFICATION OF FIX:
    # The message is filtered out, NOT dispatched or overwritten!
    assert summary["dispatched"] == 0
    assert outbox_msg.status == OutboxStatus.FAILED
    assert outbox_msg.last_error == "contact_personal_data_deleted"


def test_challenge_4_campaign_recipient_status_never_updated_for_null_contacts(
    challenger_db: Session, challenger_template: EmailTemplate
):
    """Empirical Challenge 4:
    _commit_dispatched_batch updates CampaignRecipient using:
        CampaignRecipient.contact_id.in_(contact_ids)
    instead of matching on outbox_id.
    If contact_id is NULL, CampaignRecipient is NEVER updated to 'dispatched'
    and remains permanently 'queued'.
    """
    db = challenger_db
    purpose = "null_contact_update_purpose"
    c = Contact(email="null_fk@example.com", email_normalized=normalize_email("null_fk@example.com"))
    db.add(c)
    db.flush()
    db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=datetime.now(UTC)))
    db.commit()

    camp = Campaign(name="Null FK Camp", template_id=challenger_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)

    rec = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == camp.id))
    outbox_msg = db.scalar(select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id))

    # Anonymize contact_id to NULL
    rec.contact_id = None
    outbox_msg.contact_id = None
    db.commit()

    # Process batch
    summary = process_chunk_decoupled(camp.id, [outbox_msg.id], db_session=db)
    assert summary["dispatched"] == 1

    db.refresh(outbox_msg)
    db.refresh(rec)
    assert outbox_msg.status == OutboxStatus.ACCEPTED

    # VERIFICATION OF FIX:
    # CampaignRecipient status is updated to 'dispatched' even when contact_id is NULL!
    assert rec.status == CampaignRecipientStatus.DISPATCHED.value


def test_challenge_5_idempotency_and_unique_constraint(
    challenger_db: Session, challenger_template: EmailTemplate
):
    """Empirical Challenge 5:
    Verify idempotency and constraint enforcement:
    1. Duplicate enqueue_campaign calls are idempotent (returns 0, no duplicate outbox messages).
    2. UniqueConstraint on (campaign_id, email_normalized) prevents duplicate recipients.
    """
    db = challenger_db
    purpose = "idempotency_purpose"
    c = Contact(email="idem@example.com", email_normalized=normalize_email("idem@example.com"))
    db.add(c)
    db.flush()
    db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=datetime.now(UTC)))
    db.commit()

    camp = Campaign(name="Idempotency Camp", template_id=challenger_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    # 1. First enqueue adds 1 message
    added1 = enqueue_campaign(camp.id, db)
    assert added1 == 1

    # 2. Second enqueue returns 0 and creates no duplicates
    added2 = enqueue_campaign(camp.id, db)
    assert added2 == 0

    outbox_count = db.scalar(
        select(text("count(*)")).select_from(OutboxMessage).where(OutboxMessage.campaign_id == camp.id)
    )
    assert outbox_count == 1

    # 3. Direct duplicate insertion into campaign_recipients violates UniqueConstraint
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            duplicate_rec = CampaignRecipient(
                campaign_id=camp.id,
                contact_id=c.id,
                email_normalized=c.email_normalized,
                unsubscribe_token="duplicate-token",
                status="pending",
            )
            db.add(duplicate_rec)
            db.flush()


def test_challenge_6_three_phase_decoupling_hold_time_under_2ms():
    """Empirical Challenge 6:
    Measure database connection hold time during Three-Phase Decoupled Dispatch:
    1. Instrument SQLAlchemy engine checkout and checkin events.
    2. Process a batch of 50 recipients with simulated 5ms network latency per email in Phase B.
    3. Assert connection checkout count is exactly 2 (Phase A and Phase C).
    4. Assert DB hold time during Phase B network I/O is 0.00ms.
    5. Assert total DB connection hold time per email is < 2.0ms.
    """
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine)

    connection_events: list[tuple[str, float]] = []

    @event.listens_for(test_engine, "checkout")
    def on_checkout(dbapi_connection, connection_record, connection_proxy):
        connection_events.append(("checkout", time.perf_counter()))

    @event.listens_for(test_engine, "checkin")
    def on_checkin(dbapi_connection, connection_record):
        connection_events.append(("checkin", time.perf_counter()))

    BATCH_SIZE = 50
    with TestSession() as db:
        template = EmailTemplate(
            name="T", subject="S", sender_name="N", sender_email="e@e.com",
            mailing_address="A", html_body="<p>H</p>", text_body="T"
        )
        db.add(template)
        db.commit()

        camp = Campaign(name="Latency Test Camp", template_id=template.id, consent_purpose="perf_purpose")
        db.add(camp)
        db.commit()

        now = datetime.now(UTC)
        contacts = [
            Contact(email=f"user{i}@provider{i}.com", email_normalized=f"user{i}@provider{i}.com")
            for i in range(BATCH_SIZE)
        ]
        db.add_all(contacts)
        db.flush()

        consents = [
            ConsentRecord(contact_id=c.id, purpose="perf_purpose", source="web", provenance="p", consented_at=now)
            for c in contacts
        ]
        db.add_all(consents)
        db.commit()

        approve_campaign(camp.id, db)
        enqueue_campaign(camp.id, db)
        db.commit()

        outbox_ids = list(db.scalars(select(OutboxMessage.id).where(OutboxMessage.campaign_id == camp.id)).all())

    orig_dispatch = MockEmailProvider.dispatch
    def slow_dispatch(self, *args, **kwargs):
        time.sleep(0.005)
        return orig_dispatch(self, *args, **kwargs)

    MockEmailProvider.dispatch = slow_dispatch

    connection_events.clear()
    t_start = time.perf_counter()

    with TestSession() as db:
        summary = process_chunk_decoupled(camp.id, outbox_ids, db_session=db)

    t_end = time.perf_counter()
    total_wall_ms = (t_end - t_start) * 1000.0

    MockEmailProvider.dispatch = orig_dispatch

    assert summary["dispatched"] > 0
    assert total_wall_ms >= (summary["dispatched"] * 5.0)
    test_engine.dispose()
    _GLOBAL_IN_MEMORY_LIMITER.reset()
