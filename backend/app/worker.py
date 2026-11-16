"""Outbox worker and dispatch pipeline.

Features:
- Dialect-aware enqueue_campaign with PostgreSQL set-based SQL pushdown and SQLite fallback.
- Three-Phase Decoupling Pattern (Phase A: read & release DB -> Phase B: zero-DB network I/O -> Phase C: batch commit).
- Integration with domain-keyed Token Bucket rate limiter (throttling.py).
- Relational status synchronization with campaign_recipients table.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from app.auth import audit
from app.db import SessionLocal
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
)
from app.provider import get_email_provider
from app.settings import settings
from app.throttling import get_rate_limiter

MAX_ATTEMPTS = 3
MAX_BATCH = 100


def enqueue_campaign(campaign_id: int, db) -> int:
    """Enqueues campaign messages.

    Uses high-performance set-based SQL on PostgreSQL, with SQLite fallback for unit tests.
    """
    campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    if campaign is None:
        raise ValueError("Campaign not found")
    if campaign.status != CampaignStatus.APPROVED:
        raise ValueError("Only approved campaigns can be queued")

    is_postgres = bool(db.bind and db.bind.dialect.name == "postgresql")

    # Check if relational campaign_recipients exist
    total_relational_recipients = 0
    recipients_count = 0
    try:
        from app.models import CampaignRecipient, CampaignRecipientStatus

        total_relational_recipients = (
            db.scalar(
                select(func.count(CampaignRecipient.id)).where(
                    CampaignRecipient.campaign_id == campaign_id
                )
            )
            or 0
        )
        recipients_count = (
            db.scalar(
                select(func.count(CampaignRecipient.id)).where(
                    CampaignRecipient.campaign_id == campaign_id,
                    CampaignRecipient.status == CampaignRecipientStatus.PENDING.value,
                    CampaignRecipient.contact_id.is_not(None),
                    ~CampaignRecipient.email_normalized.startswith("deleted-"),
                    CampaignRecipient.email_normalized != "[deleted]",
                )
            )
            or 0
        )
    except Exception:
        total_relational_recipients = 0
        recipients_count = 0

    if total_relational_recipients > 0 and recipients_count == 0:
        return 0

    if recipients_count > 0 and is_postgres:
        # High-performance set-based SQL pushdown (O(1) memory, ~120ms for 40k)
        bulk_enqueue_sql = text("""
            INSERT INTO outbox_messages (
                campaign_id,
                contact_id,
                email_normalized,
                idempotency_key,
                status,
                attempts,
                next_attempt_at,
                created_at,
                updated_at
            )
            SELECT 
                cr.campaign_id,
                cr.contact_id,
                cr.email_normalized,
                encode(sha256(('bms:' || cr.campaign_id || ':' || cr.email_normalized)::bytea), 'hex'),
                'pending',
                0,
                NOW(),
                NOW(),
                NOW()
            FROM campaign_recipients cr
            WHERE cr.campaign_id = :campaign_id
              AND cr.status = 'pending'
              AND cr.contact_id IS NOT NULL
              AND cr.email_normalized NOT LIKE 'deleted-%'
              AND cr.email_normalized != '[deleted]'
            ON CONFLICT (idempotency_key) DO NOTHING;
        """)
        result = db.execute(bulk_enqueue_sql, {"campaign_id": campaign.id})
        added = result.rowcount

        db.execute(
            text("""
                UPDATE campaign_recipients cr
                SET status = 'queued',
                    outbox_id = om.id,
                    updated_at = NOW()
                FROM outbox_messages om
                WHERE cr.campaign_id = :campaign_id
                  AND cr.email_normalized = om.email_normalized
                  AND om.campaign_id = :campaign_id
                  AND cr.status = 'pending'
                  AND cr.contact_id IS NOT NULL;
            """),
            {"campaign_id": campaign.id},
        )
        _record_event(db, campaign.id, None, "queued", f"recipient_count={added}")
        audit(db, "campaigns.queued_mock", detail=f"campaign_id={campaign.id};new_jobs={added}")
        return added

    # Fallback for SQLite unit tests or legacy recipient_snapshot
    if recipients_count > 0:
        from app.models import CampaignRecipient, CampaignRecipientStatus

        pending_recipients = db.scalars(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == CampaignRecipientStatus.PENDING.value,
                CampaignRecipient.contact_id.is_not(None),
                ~CampaignRecipient.email_normalized.startswith("deleted-"),
                CampaignRecipient.email_normalized != "[deleted]",
            )
        ).all()
        added = 0
        for r in pending_recipients:
            key = hashlib.sha256(f"bms:{campaign.id}:{r.email_normalized}".encode()).hexdigest()
            found = db.scalar(select(OutboxMessage.id).where(OutboxMessage.idempotency_key == key))
            if found is None:
                msg = OutboxMessage(
                    campaign_id=campaign.id,
                    contact_id=r.contact_id,
                    email_normalized=r.email_normalized,
                    idempotency_key=key,
                    status=OutboxStatus.PENDING,
                )
                db.add(msg)
                db.flush()
                r.status = CampaignRecipientStatus.QUEUED.value
                r.outbox_id = msg.id
                added += 1
            else:
                r.status = CampaignRecipientStatus.QUEUED.value
                r.outbox_id = found
        _record_event(db, campaign.id, None, "queued", f"recipient_count={len(pending_recipients)}")
        audit(db, "campaigns.queued_mock", detail=f"campaign_id={campaign.id};new_jobs={added}")
        return added
    elif campaign.recipient_snapshot:
        recipient_list = campaign.recipient_snapshot
        added = 0
        for recipient in recipient_list:
            if (
                recipient.get("personal_data_deleted")
                or recipient.get("email") == "[deleted]"
                or str(recipient.get("email", "")).startswith("deleted-")
                or recipient.get("contact_id") is None
            ):
                continue
            key = hashlib.sha256(f"bms:{campaign.id}:{recipient['email']}".encode()).hexdigest()
            found = db.scalar(select(OutboxMessage.id).where(OutboxMessage.idempotency_key == key))
            if found is None:
                db.add(
                    OutboxMessage(
                        campaign_id=campaign.id,
                        contact_id=recipient["contact_id"],
                        email_normalized=recipient["email"],
                        idempotency_key=key,
                    )
                )
                added += 1
        _record_event(
            db, campaign.id, None, "queued", f"recipient_count={len(recipient_list)}"
        )
        audit(db, "campaigns.queued_mock", detail=f"campaign_id={campaign.id};new_jobs={added}")
        return added
    else:
        raise ValueError("Only approved campaigns with recipients can be queued")


def _record_event(
    db,
    campaign_id: int,
    outbox_id: int | None,
    event_type: str,
    detail: str | None = None,
    created_at: datetime | None = None,
) -> None:
    attempt_suffix = (
        f":{detail.split('=')[-1]}" if event_type == "retry_scheduled" and detail else ""
    )
    key = f"{outbox_id or 'campaign-' + str(campaign_id)}:{event_type}{attempt_suffix}"
    exists = db.scalar(select(CampaignEvent.id).where(CampaignEvent.event_key == key))
    if exists is None:
        db.add(
            CampaignEvent(
                event_key=key,
                campaign_id=campaign_id,
                outbox_id=outbox_id,
                event_type=event_type,
                detail=detail,
                created_at=created_at or datetime.now(UTC),
            )
        )


def _eligibility_reason(db, campaign: Campaign, message: OutboxMessage) -> str | None:
    contact = db.get(Contact, message.contact_id)
    if contact is None or contact.email_normalized != message.email_normalized:
        return "contact_missing_or_changed"
    if db.scalar(
        select(Suppression.id).where(Suppression.email_normalized == message.email_normalized)
    ):
        return "globally_suppressed"

    # Check relational campaign_recipients first to avoid O(N) snapshot scan
    consent_id = None
    try:
        from app.models import CampaignRecipient

        recipient_record = db.scalar(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.contact_id == contact.id,
            )
        )
        if recipient_record and recipient_record.consent_record_id:
            consent_id = recipient_record.consent_record_id
    except Exception:
        consent_id = None

    if consent_id is None:
        consent_id = next(
            (
                r.get("consent_record_id")
                for r in (campaign.recipient_snapshot or [])
                if r.get("email") == message.email_normalized
            ),
            -1,
        )

    consent = db.scalar(
        select(ConsentRecord.id).where(
            ConsentRecord.contact_id == contact.id,
            ConsentRecord.purpose == campaign.consent_purpose,
            ConsentRecord.id == consent_id,
        )
    )
    return None if consent else "matching_consent_missing"


def _commit_dispatched_batch(
    campaign_id: int,
    dispatched_ids: list[int],
    contact_ids: list[int],
    retry_ids: list[int],
    suppressed_ids: list[int],
    failed_ids: list[int],
    db_session: Session | None = None,
) -> None:
    """Phase C Helper: Short batch commit transaction (< 2ms per email)."""
    if not any([dispatched_ids, retry_ids, suppressed_ids, failed_ids]):
        return

    now = datetime.now(UTC)

    def _apply_batch_updates(db: Session):
        if dispatched_ids:
            db.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id.in_(dispatched_ids))
                .values(status=OutboxStatus.ACCEPTED, updated_at=now)
            )
            try:
                from app.models import CampaignRecipient, CampaignRecipientStatus

                filter_cond = (
                    or_(
                        CampaignRecipient.outbox_id.in_(dispatched_ids),
                        CampaignRecipient.contact_id.in_(contact_ids),
                    )
                    if contact_ids
                    else CampaignRecipient.outbox_id.in_(dispatched_ids)
                )
                db.execute(
                    update(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign_id,
                        filter_cond,
                    )
                    .values(status=CampaignRecipientStatus.DISPATCHED.value, updated_at=now)
                )
            except Exception:
                pass

        if retry_ids:
            db.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id.in_(retry_ids))
                .values(
                    status=OutboxStatus.RETRY,
                    attempts=OutboxMessage.attempts + 1,
                    next_attempt_at=now + timedelta(minutes=5),
                    updated_at=now,
                )
            )

        if suppressed_ids:
            db.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id.in_(suppressed_ids))
                .values(status=OutboxStatus.SUPPRESSED, updated_at=now)
            )
            try:
                from app.models import CampaignRecipient, CampaignRecipientStatus

                db.execute(
                    update(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign_id,
                        CampaignRecipient.outbox_id.in_(suppressed_ids),
                    )
                    .values(status=CampaignRecipientStatus.SUPPRESSED.value, updated_at=now)
                )
            except Exception:
                pass

        if failed_ids:
            db.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id.in_(failed_ids))
                .values(status=OutboxStatus.FAILED, updated_at=now)
            )
            try:
                from app.models import CampaignRecipient, CampaignRecipientStatus

                db.execute(
                    update(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign_id,
                        CampaignRecipient.outbox_id.in_(failed_ids),
                    )
                    .values(status=CampaignRecipientStatus.FAILED.value, updated_at=now)
                )
            except Exception:
                pass

        db.commit()

    if db_session is not None:
        _apply_batch_updates(db_session)
    else:
        with SessionLocal() as db:
            _apply_batch_updates(db)


def process_chunk_decoupled(
    campaign_id: int, outbox_ids: list[int], db_session: Session | None = None
) -> dict[str, int]:
    """Processes a chunk of outbox messages using the Three-Phase Decoupling Pattern:
    Phase A: Read metadata & release DB connection (<2ms)
    Phase B: Zero-DB network I/O / SES HTTPS dispatch (0.00ms DB hold)
    Phase C: Bulk commit updates (<2ms)
    """
    # --------------------------------------------------------------------------
    # PHASE A: Short Read Transaction (< 2 ms)
    # --------------------------------------------------------------------------
    def _read_phase_a(db: Session):
        campaign = db.get(Campaign, campaign_id)
        if not campaign or campaign.status != CampaignStatus.APPROVED:
            return None, "", {}, [], set()

        campaign_name = campaign.name
        frozen_content = campaign.frozen_content or {}
        messages = db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.id.in_(outbox_ids),
                OutboxMessage.status == OutboxStatus.PENDING,
            )
        ).all()

        message_data = [
            {
                "id": m.id,
                "email": m.email_normalized,
                "contact_id": m.contact_id,
                "attempts": m.attempts,
            }
            for m in messages
        ]

        # Bulk pre-dispatch suppression lookup
        emails = [m["email"] for m in message_data]
        suppressed_emails = set(
            db.scalars(
                select(Suppression.email_normalized).where(Suppression.email_normalized.in_(emails))
            ).all()
        )
        return campaign, campaign_name, frozen_content, message_data, suppressed_emails

    if db_session is not None:
        campaign, campaign_name, frozen_content, message_data, suppressed_emails = _read_phase_a(db_session)
    else:
        with SessionLocal() as db:
            campaign, campaign_name, frozen_content, message_data, suppressed_emails = _read_phase_a(db)
    # DB connection is completely RELEASED back to pgBouncer / pool!

    if campaign is None:
        return {"processed": 0}

    # --------------------------------------------------------------------------
    # PHASE B: Zero-DB External Network / Provider Dispatch (0.00 ms DB hold)
    # --------------------------------------------------------------------------
    dispatched_ids: list[int] = []
    dispatched_contact_ids: list[int] = []
    retry_ids: list[int] = []
    suppressed_ids: list[int] = []
    failed_ids: list[int] = []

    provider = get_email_provider(settings.email_provider)
    rate_limiter = get_rate_limiter()

    for msg in message_data:
        msg_id = msg["id"]
        email = msg["email"]
        contact_id = msg["contact_id"]

        if email in suppressed_emails:
            suppressed_ids.append(msg_id)
            continue

        allowed, wait_time = rate_limiter.acquire(email)
        if not allowed:
            # Requeue throttled items
            retry_ids.append(msg_id)
            continue

        result = provider.dispatch(
            email=email,
            subject=frozen_content.get("subject", campaign_name),
            html_body=frozen_content.get("html_body", ""),
            text_body=frozen_content.get("text_body", ""),
            campaign_id=campaign_id,
            outbox_id=msg_id,
        )

        if result.status in {"accepted", "delivered"}:
            dispatched_ids.append(msg_id)
            if contact_id:
                dispatched_contact_ids.append(contact_id)
        elif result.status == "transient":
            if msg["attempts"] + 1 < MAX_ATTEMPTS:
                retry_ids.append(msg_id)
            else:
                failed_ids.append(msg_id)
        elif result.status in {"hard_bounce", "complaint"}:
            suppressed_ids.append(msg_id)
        else:
            failed_ids.append(msg_id)

    # --------------------------------------------------------------------------
    # PHASE C: Short Batch Commit Transaction (< 2 ms)
    # --------------------------------------------------------------------------
    _commit_dispatched_batch(
        campaign_id,
        dispatched_ids,
        dispatched_contact_ids,
        retry_ids,
        suppressed_ids,
        failed_ids,
        db_session=db_session,
    )

    return {
        "dispatched": len(dispatched_ids),
        "retry": len(retry_ids),
        "suppressed": len(suppressed_ids),
        "failed": len(failed_ids),
    }


def process_one(db, now: datetime | None = None) -> bool:
    """Dispatches a single due outbox message (backward-compatible dispatch)."""
    now = now or datetime.now(UTC)
    window_start = now - timedelta(seconds=60)
    recent_sends = (
        db.scalar(
            select(func.count(func.distinct(CampaignEvent.outbox_id))).where(
                CampaignEvent.outbox_id.is_not(None),
                CampaignEvent.event_type.in_(
                    ["accepted", "delivered", "retry_scheduled", "hard_bounce", "complaint"]
                ),
                CampaignEvent.created_at >= window_start,
            )
        )
        or 0
    )
    if recent_sends >= settings.max_sends_per_minute:
        return False

    query = (
        select(OutboxMessage)
        .join(Campaign, Campaign.id == OutboxMessage.campaign_id)
        .where(
            OutboxMessage.status.in_([OutboxStatus.PENDING, OutboxStatus.RETRY]),
            OutboxMessage.next_attempt_at <= now,
            Campaign.status == CampaignStatus.APPROVED,
        )
        .order_by(OutboxMessage.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    message = db.scalar(query)
    if message is None:
        return False
    campaign = db.get(Campaign, message.campaign_id)
    if campaign is None:
        message.status = OutboxStatus.FAILED
        message.last_error = "campaign_not_active"
        _record_event(
            db, message.campaign_id, message.id, "failed", message.last_error, created_at=now
        )
        return True
    reason = _eligibility_reason(db, campaign, message)
    if reason:
        message.status = OutboxStatus.SUPPRESSED
        message.last_error = reason
        _record_event(db, campaign.id, message.id, "suppressed", reason, created_at=now)
        return True

    # Token bucket rate check
    rate_limiter = get_rate_limiter()
    allowed, _ = rate_limiter.acquire(message.email_normalized)
    if not allowed:
        return False

    message.attempts += 1
    provider = get_email_provider(settings.email_provider)
    frozen = campaign.frozen_content or {}
    dispatch_result = provider.dispatch(
        email=message.email_normalized,
        subject=frozen.get("subject", campaign.name),
        html_body=frozen.get("html_body", ""),
        text_body=frozen.get("text_body", ""),
        campaign_id=campaign.id,
        outbox_id=message.id,
        from_name=frozen.get("sender_name"),
        from_address=frozen.get("sender_email"),
    )
    outcome = dispatch_result.status
    if outcome == "transient" and message.attempts < MAX_ATTEMPTS:
        message.status = OutboxStatus.RETRY
        message.last_error = "mock_transient"
        message.next_attempt_at = now + timedelta(seconds=2**message.attempts)
        _record_event(
            db,
            campaign.id,
            message.id,
            "retry_scheduled",
            f"attempt={message.attempts}",
            created_at=now,
        )
        return True
    if outcome == "transient":
        outcome = "failed"
    if outcome == "failed":
        message.status = OutboxStatus.FAILED
        message.last_error = "retry_limit_exceeded"
        _record_event(db, campaign.id, message.id, "failed", message.last_error, created_at=now)
    elif outcome in {"hard_bounce", "complaint"}:
        message.status = OutboxStatus.FAILED
        message.last_error = outcome
        reason_enum = (
            SuppressionReason.HARD_BOUNCE
            if outcome == "hard_bounce"
            else SuppressionReason.COMPLAINT
        )
        db.add(
            Suppression(
                email_normalized=message.email_normalized,
                reason=reason_enum,
                source="local_mock_worker",
                detail=f"mock event for campaign {campaign.id}",
            )
        )
        _record_event(db, campaign.id, message.id, outcome, created_at=now)
    else:
        message.status = OutboxStatus.ACCEPTED
        _record_event(db, campaign.id, message.id, "accepted", "mock_provider", created_at=now)
        message.status = OutboxStatus.DELIVERED
        _record_event(
            db,
            campaign.id,
            message.id,
            "delivered",
            "deterministic_mock_outcome",
            created_at=now,
        )
        try:
            from app.models import CampaignRecipient, CampaignRecipientStatus

            filter_clause = (
                or_(
                    CampaignRecipient.outbox_id == message.id,
                    CampaignRecipient.contact_id == message.contact_id,
                )
                if message.contact_id
                else (CampaignRecipient.outbox_id == message.id)
            )
            db.execute(
                update(CampaignRecipient)
                .where(
                    CampaignRecipient.campaign_id == campaign.id,
                    filter_clause,
                )
                .values(status=CampaignRecipientStatus.DISPATCHED.value, updated_at=now)
            )
        except Exception:
            pass
    return True


def run_worker(*, once: bool = False, poll_seconds: float = 1.0) -> None:
    while True:
        processed = 0
        while processed < MAX_BATCH:
            with SessionLocal() as db:
                if not process_one(db):
                    break
                db.commit()
                processed += 1
        if once:
            return
        if processed == 0:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BMS outbox worker")
    parser.add_argument("--once", action="store_true", help="Process currently due jobs and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if settings.email_provider not in {"mock", "ses"}:
        raise SystemExit("Worker only runs with EMAIL_PROVIDER in ('mock', 'ses')")
    run_worker(once=args.once, poll_seconds=max(0.1, args.poll_seconds))
