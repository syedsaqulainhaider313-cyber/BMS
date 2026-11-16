"""Phase 1: Database & Recipient Pipeline Modernization Unit & Integration Tests.

Validates:
1. campaign_recipients table creation, columns, indexes, and unique constraints.
2. GDPR compliance: ON DELETE SET NULL on contact_id and consent_record_id.
3. ON DELETE CASCADE on campaign_id.
4. Standalone index ix_campaign_events_created_at on campaign_events.
5. campaign_eligibility SQL pushdown preserving eligible and excluded (with reasons).
6. approve_campaign populating campaign_recipients junction table.
7. enqueue_campaign pushdown populating outbox_messages and updating recipient statuses.
8. Three-Phase Worker Decoupling (process_chunk_decoupled and _commit_dispatched_batch).
9. db.py QueuePool configuration and prepare_threshold: None for PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.db import get_engine_kwargs
from app.main import approve_campaign, campaign_eligibility, delete_contact
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
from app.worker import _commit_dispatched_batch, enqueue_campaign, process_chunk_decoupled


def test_campaign_recipients_table_schema(db: Session):
    """Verify campaign_recipients table exists with all required columns and indexes."""
    inspector = inspect(db.bind)
    table_names = inspector.get_table_names()
    assert "campaign_recipients" in table_names

    columns = {col["name"]: col for col in inspector.get_columns("campaign_recipients")}
    expected_cols = [
        "id",
        "campaign_id",
        "contact_id",
        "email_normalized",
        "consent_record_id",
        "unsubscribe_token",
        "status",
        "outbox_id",
        "created_at",
        "updated_at",
    ]
    for col_name in expected_cols:
        assert col_name in columns, f"Missing column {col_name} in campaign_recipients"

    indexes = {idx["name"]: idx for idx in inspector.get_indexes("campaign_recipients")}
    assert "ix_campaign_recipients_campaign_status" in indexes
    assert "ix_campaign_recipients_contact" in indexes
    assert "ix_campaign_recipients_email" in indexes
    assert "ix_campaign_recipients_outbox" in indexes

    event_indexes = {idx["name"]: idx for idx in inspector.get_indexes("campaign_events")}
    assert "ix_campaign_events_created_at" in event_indexes


def test_campaign_recipients_gdpr_foreign_key_set_null(db: Session, sample_template: EmailTemplate):
    """Verify GDPR requirement: foreign keys define ON DELETE SET NULL and delete_contact anonymizes junction records."""
    from app.main import delete_contact

    # 1. Verify schema DDL foreign key constraints specify ON DELETE SET NULL and CASCADE
    inspector = inspect(db.bind)
    fks = {tuple(fk["constrained_columns"]): fk for fk in inspector.get_foreign_keys("campaign_recipients")}
    assert fks[("contact_id",)]["options"].get("ondelete") == "SET NULL"
    assert fks[("consent_record_id",)]["options"].get("ondelete") == "SET NULL"
    assert fks[("campaign_id",)]["options"].get("ondelete") == "CASCADE"

    # 2. Verify application GDPR endpoint anonymization and contact FK nullification
    contact = Contact(email="gdpr_test@example.com", email_normalized=normalize_email("gdpr_test@example.com"))
    db.add(contact)
    db.flush()

    consent = ConsentRecord(
        contact_id=contact.id,
        purpose="updates",
        source="form",
        provenance="ip=127.0.0.1",
        consented_at=datetime.now(UTC),
    )
    db.add(consent)
    db.flush()

    campaign = Campaign(name="GDPR Camp", template_id=sample_template.id, consent_purpose="updates")
    db.add(campaign)
    db.flush()

    recipient = CampaignRecipient(
        campaign_id=campaign.id,
        contact_id=contact.id,
        consent_record_id=consent.id,
        email_normalized=contact.email_normalized,
        unsubscribe_token="test-token-123",
        status=CampaignRecipientStatus.PENDING.value,
    )
    db.add(recipient)
    db.commit()

    # Call delete_contact GDPR endpoint
    res = delete_contact(contact.id, db)
    assert res["status"] == "deleted"

    # Verify recipient record is preserved for campaign accounting but PII is wiped & FKs set to NULL
    db.refresh(recipient)
    assert recipient.contact_id is None
    assert recipient.consent_record_id is None
    assert recipient.email_normalized == f"deleted-contact-{contact.id}"
    assert recipient.status == CampaignRecipientStatus.CANCELLED.value
    assert recipient.campaign_id == campaign.id




def test_campaign_recipients_cascade_deletion(db: Session, sample_template: EmailTemplate):
    """Verify deleting a Campaign cascades and purges its campaign_recipients."""
    campaign = Campaign(name="Purge Camp", template_id=sample_template.id, consent_purpose="news")
    db.add(campaign)
    db.flush()

    recipient = CampaignRecipient(
        campaign_id=campaign.id,
        contact_id=None,
        email_normalized="cascade@example.com",
        unsubscribe_token="token-cascade",
        status=CampaignRecipientStatus.PENDING.value,
    )
    db.add(recipient)
    db.commit()

    rec_id = recipient.id
    db.delete(campaign)
    db.commit()

    assert db.get(CampaignRecipient, rec_id) is None


def test_sql_pushdown_campaign_eligibility_breakdown(db: Session, sample_template: EmailTemplate):
    """Verify SQL pushdown separates eligible from excluded with correct reason breakdown."""
    purpose = "product_announcements"
    c_eligible = Contact(email="eligible@test.org", email_normalized=normalize_email("eligible@test.org"))
    c_suppressed = Contact(email="suppressed@test.org", email_normalized=normalize_email("suppressed@test.org"))
    c_no_consent = Contact(email="noconsent@test.org", email_normalized=normalize_email("noconsent@test.org"))
    db.add_all([c_eligible, c_suppressed, c_no_consent])
    db.flush()

    # Consent for eligible and suppressed
    db.add(ConsentRecord(contact_id=c_eligible.id, purpose=purpose, source="web", provenance="optin", consented_at=datetime.now(UTC)))
    db.add(ConsentRecord(contact_id=c_suppressed.id, purpose=purpose, source="web", provenance="optin", consented_at=datetime.now(UTC)))
    # Suppression for suppressed
    db.add(Suppression(email_normalized=c_suppressed.email_normalized, reason=SuppressionReason.UNSUBSCRIBE, source="manual"))
    db.commit()

    campaign = Campaign(name="Pushdown Camp", template_id=sample_template.id, consent_purpose=purpose)
    db.add(campaign)
    db.commit()

    report = campaign_eligibility(campaign, db)
    assert report["eligible_count"] == 1
    assert report["excluded_count"] == 2

    eligible_emails = [e["email"] for e in report["eligible"]]
    assert "eligible@test.org" in eligible_emails

    excluded_map = {item["contact_id"]: item["reason"] for item in report["excluded"]}
    assert excluded_map[c_suppressed.id] == "globally_suppressed"
    assert excluded_map[c_no_consent.id] == "missing_matching_consent"


def test_approve_and_enqueue_campaign_junction_pipeline(db: Session, sample_template: EmailTemplate):
    """Verify approve_campaign populates campaign_recipients and enqueue_campaign creates outbox_messages."""
    purpose = "newsletter_pipeline"
    contact = Contact(email="subscriber@example.com", email_normalized=normalize_email("subscriber@example.com"))
    db.add(contact)
    db.flush()
    db.add(ConsentRecord(contact_id=contact.id, purpose=purpose, source="api", provenance="test", consented_at=datetime.now(UTC)))
    db.commit()

    campaign = Campaign(name="Pipeline Test", template_id=sample_template.id, consent_purpose=purpose)
    db.add(campaign)
    db.commit()

    # 1. Approve Campaign
    out = approve_campaign(campaign.id, db)
    assert out["status"] == "approved"
    assert out["recipient_count"] == 1

    # Verify campaign_recipients populated
    recipients = db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)).all()
    assert len(recipients) == 1
    rec = recipients[0]
    assert rec.status == CampaignRecipientStatus.PENDING.value
    assert rec.email_normalized == "subscriber@example.com"
    assert rec.contact_id == contact.id

    # 2. Enqueue Campaign
    added = enqueue_campaign(campaign.id, db)
    assert added == 1

    # Verify status changed to queued and outbox_id linked
    db.refresh(rec)
    assert rec.status == CampaignRecipientStatus.QUEUED.value
    assert rec.outbox_id is not None

    outbox_msg = db.get(OutboxMessage, rec.outbox_id)
    assert outbox_msg is not None
    assert outbox_msg.email_normalized == "subscriber@example.com"
    assert outbox_msg.campaign_id == campaign.id


def test_three_phase_decoupling_pipeline(db: Session, sample_template: EmailTemplate):
    """Verify Three-Phase Decoupling: read -> external I/O -> batch commit."""
    purpose = "decoupling_test"
    c1 = Contact(email="d1@example.com", email_normalized=normalize_email("d1@example.com"))
    c2 = Contact(email="d2@example.com", email_normalized=normalize_email("d2@example.com"))
    db.add_all([c1, c2])
    db.flush()
    now = datetime.now(UTC)
    db.add_all([
        ConsentRecord(contact_id=c1.id, purpose=purpose, source="web", provenance="p", consented_at=now),
        ConsentRecord(contact_id=c2.id, purpose=purpose, source="web", provenance="p", consented_at=now),
    ])
    db.commit()

    campaign = Campaign(name="Decouple Camp", template_id=sample_template.id, consent_purpose=purpose)
    db.add(campaign)
    db.commit()

    approve_campaign(campaign.id, db)
    enqueue_campaign(campaign.id, db)

    outbox_ids = list(db.scalars(select(OutboxMessage.id).where(OutboxMessage.campaign_id == campaign.id)).all())
    assert len(outbox_ids) == 2

    # Process via decoupled chunk processor
    summary = process_chunk_decoupled(campaign.id, outbox_ids, db_session=db)
    assert summary["dispatched"] == 2
    assert summary["failed"] == 0

    # Verify outbox messages accepted
    messages = db.scalars(select(OutboxMessage).where(OutboxMessage.id.in_(outbox_ids))).all()
    for m in messages:
        assert m.status == OutboxStatus.ACCEPTED

    # Verify campaign_recipients dispatched
    recs = db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)).all()
    for r in recs:
        assert r.status == CampaignRecipientStatus.DISPATCHED.value


def test_db_engine_kwargs_and_pgbouncer_compatibility(monkeypatch):
    """Verify QueuePool settings and prepare_threshold: None for PostgreSQL."""
    from app.settings import settings

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://bms:secret@localhost:5432/bms")
    monkeypatch.setattr(settings, "db_pool_size", 10)
    monkeypatch.setattr(settings, "db_max_overflow", 15)
    monkeypatch.setattr(settings, "db_pool_recycle", 1800)
    monkeypatch.setattr(settings, "db_pool_timeout", 30)

    kwargs = get_engine_kwargs()
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 10
    assert kwargs["max_overflow"] == 15
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["pool_timeout"] == 30
    assert kwargs["connect_args"]["prepare_threshold"] is None


def test_defect_1_multi_contact_deletion_unique_constraint(db: Session, sample_template: EmailTemplate):
    """Verify multiple contacts in the same campaign can be deleted under GDPR
    without triggering a unique constraint collision or retaining PII."""
    purpose = "gdpr_multi_test"
    c1 = Contact(email="user1_gdpr@example.com", email_normalized=normalize_email("user1_gdpr@example.com"))
    c2 = Contact(email="user2_gdpr@example.com", email_normalized=normalize_email("user2_gdpr@example.com"))
    c3 = Contact(email="user3_gdpr@example.com", email_normalized=normalize_email("user3_gdpr@example.com"))
    db.add_all([c1, c2, c3])
    db.flush()

    now = datetime.now(UTC)
    for c in [c1, c2, c3]:
        db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="form", provenance="p", consented_at=now))
    db.commit()

    camp = Campaign(name="Multi GDPR Campaign", template_id=sample_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    c1_id, c2_id, c3_id = c1.id, c2.id, c3.id

    res1 = delete_contact(c1_id, db)
    assert res1["status"] == "deleted"

    res2 = delete_contact(c2_id, db)
    assert res2["status"] == "deleted"

    res3 = delete_contact(c3_id, db)
    assert res3["status"] == "deleted"

    assert db.get(Contact, c1_id) is None
    assert db.get(Contact, c2_id) is None
    assert db.get(Contact, c3_id) is None

    recipients = list(db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == camp.id)).all())
    assert len(recipients) == 3

    surrogates = {r.email_normalized for r in recipients}
    expected_surrogates = {
        f"deleted-contact-{c1_id}",
        f"deleted-contact-{c2_id}",
        f"deleted-contact-{c3_id}",
    }
    assert surrogates == expected_surrogates
    for r in recipients:
        assert r.contact_id is None
        assert r.consent_record_id is None
        assert r.status == CampaignRecipientStatus.CANCELLED.value

    for personal_email in ["user1_gdpr@example.com", "user2_gdpr@example.com", "user3_gdpr@example.com"]:
        found = db.scalar(select(CampaignRecipient).where(CampaignRecipient.email_normalized == personal_email))
        assert found is None


def test_defect_2_enqueue_skips_pre_deleted_contacts(db: Session, sample_template: EmailTemplate):
    """Verify that contacts deleted prior to enqueue are completely excluded from outbox creation."""
    purpose = "enqueue_filter_test"
    c_active = Contact(email="active_user@example.com", email_normalized=normalize_email("active_user@example.com"))
    c_deleted = Contact(email="deleted_user@example.com", email_normalized=normalize_email("deleted_user@example.com"))
    db.add_all([c_active, c_deleted])
    db.flush()

    now = datetime.now(UTC)
    for c in [c_active, c_deleted]:
        db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=now))
    db.commit()

    camp = Campaign(name="Mixed Enqueue Campaign", template_id=sample_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    del_id = c_deleted.id
    delete_contact(del_id, db)

    added = enqueue_campaign(camp.id, db)
    assert added == 1, "Only the 1 active contact should be enqueued"

    outbox = list(db.scalars(select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id)).all())
    assert len(outbox) == 1
    assert outbox[0].contact_id == c_active.id
    assert outbox[0].email_normalized == "active_user@example.com"
    assert outbox[0].status == OutboxStatus.PENDING

    assert not any("deleted" in msg.email_normalized for msg in outbox)


def test_defect_2_enqueue_all_deleted_returns_zero(db: Session, sample_template: EmailTemplate):
    """Verify that when all contacts in a campaign were deleted before enqueue, 0 jobs are queued."""
    purpose = "all_deleted_test"
    c1 = Contact(email="all_del_a@example.com", email_normalized=normalize_email("all_del_a@example.com"))
    c2 = Contact(email="all_del_b@example.com", email_normalized=normalize_email("all_del_b@example.com"))
    db.add_all([c1, c2])
    db.flush()

    now = datetime.now(UTC)
    for c in [c1, c2]:
        db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=now))
    db.commit()

    camp = Campaign(name="All Deleted Campaign", template_id=sample_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)

    delete_contact(c1.id, db)
    delete_contact(c2.id, db)

    added = enqueue_campaign(camp.id, db)
    assert added == 0

    count = db.scalar(select(func.count(OutboxMessage.id)).where(OutboxMessage.campaign_id == camp.id))
    assert count == 0


def test_defect_4_commit_dispatched_batch_syncs_suppressed_and_failed(
    db: Session, sample_template: EmailTemplate
):
    """Verify _commit_dispatched_batch updates CampaignRecipient for suppressed and failed outbox ids."""
    purpose = "sync_suppressed_failed"
    c1 = Contact(email="supp_rec@example.com", email_normalized=normalize_email("supp_rec@example.com"))
    c2 = Contact(email="fail_rec@example.com", email_normalized=normalize_email("fail_rec@example.com"))
    db.add_all([c1, c2])
    db.flush()

    now = datetime.now(UTC)
    for c in [c1, c2]:
        db.add(ConsentRecord(contact_id=c.id, purpose=purpose, source="web", provenance="p", consented_at=now))
    db.commit()

    camp = Campaign(name="Sync Status Camp", template_id=sample_template.id, consent_purpose=purpose)
    db.add(camp)
    db.commit()

    approve_campaign(camp.id, db)
    enqueue_campaign(camp.id, db)

    recs = {
        r.email_normalized: r
        for r in db.scalars(
            select(CampaignRecipient).where(CampaignRecipient.campaign_id == camp.id)
        ).all()
    }
    outbox_msgs = {
        m.email_normalized: m
        for m in db.scalars(
            select(OutboxMessage).where(OutboxMessage.campaign_id == camp.id)
        ).all()
    }

    supp_outbox_id = outbox_msgs["supp_rec@example.com"].id
    fail_outbox_id = outbox_msgs["fail_rec@example.com"].id

    _commit_dispatched_batch(
        campaign_id=camp.id,
        dispatched_ids=[],
        contact_ids=[],
        retry_ids=[],
        suppressed_ids=[supp_outbox_id],
        failed_ids=[fail_outbox_id],
        db_session=db,
    )

    db.refresh(recs["supp_rec@example.com"])
    db.refresh(recs["fail_rec@example.com"])

    assert recs["supp_rec@example.com"].status == CampaignRecipientStatus.SUPPRESSED.value
    assert recs["fail_rec@example.com"].status == CampaignRecipientStatus.FAILED.value

