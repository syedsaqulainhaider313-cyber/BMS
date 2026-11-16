from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent,
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    OutboxMessage,
    OutboxStatus,
)


class DeliverabilityCircuitBreaker:
    COMPLAINT_THRESHOLD = 0.0010  # 0.10% (Google / Yahoo spam complaint limit)
    HARD_BOUNCE_THRESHOLD = 0.0500  # 5.00% (Permanent hard bounce limit)
    MIN_SAMPLE_SIZE = 50  # Minimum dispatched emails before percentage evaluation
    ABSOLUTE_COMPLAINTS_LIMIT = 5  # Immediate safety trip count
    ABSOLUTE_BOUNCES_LIMIT = 20  # Immediate safety trip count

    @classmethod
    def evaluate_campaign(
        cls, db: Session, campaign_id: int, window_hours: int = 24
    ) -> dict[str, Any]:
        """Evaluates deliverability error rates for an active campaign within a sliding window.

        Trips circuit breaker if complaint or bounce rates exceed critical safety thresholds.
        """
        campaign = db.get(Campaign, campaign_id)
        if not campaign:
            return {"status": "skipped", "reason": "campaign_not_found"}
        if campaign.status != CampaignStatus.APPROVED:
            return {"status": "skipped", "reason": "campaign_not_approved", "current_status": campaign.status}

        window_start = datetime.now(UTC) - timedelta(hours=window_hours)
        rows = db.execute(
            select(CampaignEvent.event_type, func.count(CampaignEvent.id))
            .where(
                CampaignEvent.campaign_id == campaign_id,
                CampaignEvent.created_at >= window_start,
            )
            .group_by(CampaignEvent.event_type)
        ).all()
        counts: dict[str, int] = {event_type: count for event_type, count in rows}

        sent = counts.get("accepted", 0) + counts.get("delivered", 0)
        complaints = counts.get("complaint", 0)
        hard_bounces = counts.get("hard_bounce", 0) + counts.get("bounce", 0)

        # 1. Absolute count safety trip (bypasses cold start minimum)
        if complaints >= cls.ABSOLUTE_COMPLAINTS_LIMIT or hard_bounces >= cls.ABSOLUTE_BOUNCES_LIMIT:
            reason = (
                f"Absolute hazard limit reached: complaints={complaints} (limit={cls.ABSOLUTE_COMPLAINTS_LIMIT}), "
                f"hard_bounces={hard_bounces} (limit={cls.ABSOLUTE_BOUNCES_LIMIT})"
            )
            return cls.trip_campaign(
                db,
                campaign,
                reason=reason,
                sent=sent,
                complaints=complaints,
                hard_bounces=hard_bounces,
                complaint_rate=complaints / max(sent, 1),
                bounce_rate=hard_bounces / max(sent, 1),
            )

        # 2. Denominator cold start protection
        if sent < cls.MIN_SAMPLE_SIZE:
            return {
                "status": "active",
                "reason": "insufficient_volume",
                "sent": sent,
                "complaints": complaints,
                "hard_bounces": hard_bounces,
                "min_sample_size": cls.MIN_SAMPLE_SIZE,
            }

        complaint_rate = complaints / sent
        bounce_rate = hard_bounces / sent

        # 3. Rate-based circuit breaker trip
        if complaint_rate > cls.COMPLAINT_THRESHOLD:
            reason = f"Complaint rate {complaint_rate * 100:.3f}% exceeds threshold {cls.COMPLAINT_THRESHOLD * 100:.1f}%"
            return cls.trip_campaign(
                db,
                campaign,
                reason=reason,
                sent=sent,
                complaints=complaints,
                hard_bounces=hard_bounces,
                complaint_rate=complaint_rate,
                bounce_rate=bounce_rate,
            )

        if bounce_rate > cls.HARD_BOUNCE_THRESHOLD:
            reason = f"Hard bounce rate {bounce_rate * 100:.2f}% exceeds threshold {cls.HARD_BOUNCE_THRESHOLD * 100:.1f}%"
            return cls.trip_campaign(
                db,
                campaign,
                reason=reason,
                sent=sent,
                complaints=complaints,
                hard_bounces=hard_bounces,
                complaint_rate=complaint_rate,
                bounce_rate=bounce_rate,
            )

        return {
            "status": "healthy",
            "campaign_id": campaign_id,
            "sent": sent,
            "complaints": complaints,
            "hard_bounces": hard_bounces,
            "complaint_rate": complaint_rate,
            "bounce_rate": bounce_rate,
        }

    @classmethod
    def trip_campaign(
        cls, db: Session, campaign: Campaign, reason: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Trips the emergency circuit breaker on a campaign:

        - Transitions campaign status to PAUSED
        - Cancels or holds pending outbox messages
        - Cancels queued campaign recipients
        - Emits a high-priority security audit event
        """
        campaign.status = CampaignStatus.PAUSED

        # Cancel / fail pending outbox messages
        res_ob = db.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.campaign_id == campaign.id,
                OutboxMessage.status.in_([OutboxStatus.PENDING, OutboxStatus.RETRY]),
            )
            .values(status=OutboxStatus.FAILED, last_error="circuit_breaker_paused")
        )
        cancelled_outbox = res_ob.rowcount or 0

        # Cancel queued campaign recipients
        res_cr = db.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == CampaignRecipientStatus.QUEUED.value,
            )
            .values(status=CampaignRecipientStatus.CANCELLED.value)
        )
        cancelled_recipients = res_cr.rowcount or 0

        kwargs_str = ";".join(f"{k}={v}" for k, v in kwargs.items())
        audit_detail = (
            f"campaign_id={campaign.id};reason={reason};"
            f"cancelled_outbox={cancelled_outbox};cancelled_recipients={cancelled_recipients}"
        )
        if kwargs_str:
            audit_detail += f";{kwargs_str}"

        db.add(
            AuditEvent(
                actor="system:circuit_breaker",
                action="circuit_breaker.tripped",
                target_type="campaign",
                target_id=str(campaign.id),
                detail=audit_detail,
            )
        )

        return {
            "status": "tripped",
            "campaign_id": campaign.id,
            "reason": reason,
            "cancelled_outbox": cancelled_outbox,
            "cancelled_recipients": cancelled_recipients,
            **kwargs,
        }
