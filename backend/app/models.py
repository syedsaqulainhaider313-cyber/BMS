from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def normalize_email(address: str) -> str:
    """Validate and normalize an address for global uniqueness and suppression checks."""
    try:
        return validate_email(address.strip(), check_deliverability=False).normalized.casefold()
    except EmailNotValidError as exc:
        raise ValueError("Invalid email address") from exc


class SuppressionReason(StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    MANUAL = "manual"


class ImportStatus(StrEnum):
    PREVIEWED = "previewed"
    COMMITTED = "committed"
    REJECTED = "rejected"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    RETRY = "retry"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    BOUNCED = "bounced"
    COMPLAINED = "complained"


class CampaignRecipientStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BOUNCED = "bounced"
    COMPLAINED = "complained"


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_email_normalized", "email_normalized", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    consent_records: Mapped[list[ConsentRecord]] = relationship(back_populates="contact")


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        UniqueConstraint(
            "contact_id", "source", "consented_at", "purpose", name="uq_consent_evidence"
        ),
        Index("ix_consent_contact_time", "contact_id", "consented_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(300), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(300), nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    contact: Mapped[Contact] = relationship(back_populates="consent_records")


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (Index("ix_suppressions_email_normalized", "email_normalized", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[SuppressionReason] = mapped_column(
        Enum(SuppressionReason, name="suppression_reason"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, name="import_status"), default=ImportStatus.PREVIEWED, nullable=False
    )
    row_count: Mapped[int] = mapped_column(default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(default=0, nullable=False)
    commit_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_snapshot: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperatorSession(Base):
    __tablename__ = "operator_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(160), nullable=False)
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    mailing_address: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("email_templates.id", ondelete="RESTRICT"), nullable=False
    )
    consent_purpose: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"), default=CampaignStatus.DRAFT, nullable=False
    )
    frozen_content: Mapped[dict[str, object] | None] = mapped_column(JSON)
    recipient_snapshot: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    recipients: Mapped[list[CampaignRecipient]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "email_normalized", name="uq_campaign_recipient_email"),
        Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
        Index("ix_campaign_recipients_contact", "contact_id"),
        Index("ix_campaign_recipients_email", "email_normalized"),
        Index("ix_campaign_recipients_outbox", "outbox_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    consent_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("consent_records.id", ondelete="SET NULL"), nullable=True
    )
    unsubscribe_token: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=CampaignRecipientStatus.PENDING.value, nullable=False
    )
    outbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("outbox_messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")
    contact: Mapped[Contact | None] = relationship()
    consent_record: Mapped[ConsentRecord | None] = relationship()


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("campaign_id", "email_normalized", name="uq_outbox_campaign_recipient"),
        Index("ix_outbox_ready", "status", "next_attempt_at"),
        Index("ix_outbox_messages_email_normalized", "email_normalized"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"), default=OutboxStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CampaignEvent(Base):
    __tablename__ = "campaign_events"
    __table_args__ = (
        Index("ix_campaign_events_campaign_time", "campaign_id", "created_at"),
        Index("ix_campaign_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    outbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("outbox_messages.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
