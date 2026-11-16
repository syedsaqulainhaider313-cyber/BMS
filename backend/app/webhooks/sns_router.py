from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import audit, get_db
from app.circuit_breaker import DeliverabilityCircuitBreaker
from app.models import (
    CampaignEvent,
    OutboxMessage,
    OutboxStatus,
    SuppressionReason,
    normalize_email,
)
from app.settings import settings
from app.suppression import synchronize_recipient_state, upsert_global_suppression
from app.webhooks.sns_verifier import (
    validate_signing_cert_url,
    validate_subscribe_url,
    verify_sns_signature,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _extract_ses_tag(tags: Any, tag_name: str) -> int | None:
    """Extracts integer tag value from SES EmailTags formats."""
    if not isinstance(tags, dict):
        return None

    # Format 1: direct key-value {"campaign_id": "1"} or {"campaign_id": ["1"]}
    val = tags.get(tag_name)
    if val is not None:
        if isinstance(val, list) and len(val) > 0:
            val = val[0]
        try:
            return int(val)
        except (ValueError, TypeError):
            pass

    # Format 2: ses:tags list [{"Name": "campaign_id", "Value": "1"}]
    ses_tags = tags.get("ses:tags")
    if isinstance(ses_tags, list):
        for item in ses_tags:
            if isinstance(item, dict) and item.get("Name") == tag_name:
                item_val = item.get("Value")
                if item_val is not None:
                    try:
                        return int(item_val)
                    except (ValueError, TypeError):
                        pass

    return None


def process_sns_notification(raw_body: bytes, db: Session) -> dict[str, Any]:
    """Synchronous core processor for AWS SNS HTTP/HTTPS notifications.

    Decoupled from async Request reading to allow both FastAPI routing and direct synchronous test execution.
    """
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    msg_type = payload.get("Type")
    if not msg_type:
        raise HTTPException(status_code=400, detail="Missing SNS message Type")

    # 1. Strict Security & SSRF Defense on SigningCertURL
    cert_url = payload.get("SigningCertURL", "")
    try:
        validate_signing_cert_url(cert_url)
    except ValueError as exc:
        audit(db, "webhooks.rejected", detail=f"invalid_cert_url:{exc}")
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 2. Cryptographic Signature Verification
    if not verify_sns_signature(payload):
        audit(db, "webhooks.rejected", detail="invalid_sns_signature")
        db.commit()
        raise HTTPException(status_code=403, detail="Invalid AWS SNS cryptographic signature")

    # 3. Handle SubscriptionConfirmation
    if msg_type == "SubscriptionConfirmation":
        subscribe_url = payload.get("SubscribeURL", "")
        try:
            validate_subscribe_url(subscribe_url)
        except ValueError as exc:
            audit(db, "webhooks.rejected", detail=f"invalid_subscribe_url:{exc}")
            db.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        topic_arn = payload.get("TopicArn", "")
        # Auto-confirm subscription
        if settings.email_provider == "mock":
            # Mock mode: auto-confirm directly without outbound network request
            audit(db, "sns.subscription_confirmed", detail=f"topic={topic_arn};mode=mock")
            db.commit()
            return {"status": "confirmed", "mode": "mock", "topic_arn": topic_arn}
        else:
            # Live mode: execute GET request to SubscribeURL
            try:
                resp = httpx.get(subscribe_url, timeout=5.0, follow_redirects=False)
                resp.raise_for_status()
                audit(db, "sns.subscription_confirmed", detail=f"topic={topic_arn};status={resp.status_code}")
                db.commit()
                return {"status": "confirmed", "mode": "live", "topic_arn": topic_arn}
            except Exception as exc:
                audit(db, "sns.confirmation_failed", detail=f"topic={topic_arn};error={exc}")
                db.commit()
                raise HTTPException(status_code=502, detail=f"Subscription confirmation failed: {exc}") from exc

    # 4. Handle UnsubscribeConfirmation
    if msg_type == "UnsubscribeConfirmation":
        topic_arn = payload.get("TopicArn", "")
        audit(db, "sns.unsubscribe_confirmed", detail=f"topic={topic_arn}")
        db.commit()
        return {"status": "unsubscribed", "topic_arn": topic_arn}

    # 5. Handle Notification (SES event)
    if msg_type == "Notification":
        raw_message = payload.get("Message", "")
        if isinstance(raw_message, str):
            try:
                message = json.loads(raw_message)
            except Exception:
                message = {"raw": raw_message}
        elif isinstance(raw_message, dict):
            message = raw_message
        else:
            message = {}

        event_type = message.get("eventType") or message.get("notificationType") or "unknown"
        mail = message.get("mail", {})
        tags = mail.get("tags", {})
        campaign_id = _extract_ses_tag(tags, "campaign_id")
        outbox_id = _extract_ses_tag(tags, "outbox_id")
        sns_message_id = payload.get("MessageId", "unknown")

        processed_count = 0

        # A. Bounce Event (Hard vs Soft)
        if event_type == "Bounce":
            bounce_info = message.get("bounce", {})
            bounce_type = bounce_info.get("bounceType", "Permanent")
            bounced_recipients = bounce_info.get("bouncedRecipients", [])
            recipient_emails = [
                r.get("emailAddress") for r in bounced_recipients if r.get("emailAddress")
            ]
            if not recipient_emails and mail.get("destination"):
                recipient_emails = mail["destination"]

            diag_code = None
            if bounced_recipients:
                diag_code = bounced_recipients[0].get("diagnosticCode")
            if not diag_code:
                diag_code = bounce_info.get("bounceSubType", "Permanent Bounce")

            for email in recipient_emails:
                event_key = f"sns:bounce:{sns_message_id}:{email}"
                existing_event = db.scalar(
                    select(CampaignEvent.id).where(CampaignEvent.event_key == event_key)
                )
                if existing_event is not None:
                    continue

                if bounce_type == "Permanent":
                    # Hard Bounce: immediate global suppression and status sync
                    norm_email = upsert_global_suppression(
                        db,
                        email=email,
                        reason=SuppressionReason.HARD_BOUNCE,
                        source="aws_sns",
                        detail=diag_code,
                    )
                    synchronize_recipient_state(
                        db,
                        email_normalized=norm_email,
                        new_status="bounced",
                        campaign_id=campaign_id,
                        outbox_id=outbox_id,
                        error_detail=diag_code,
                    )
                    db.add(
                        CampaignEvent(
                            event_key=event_key,
                            campaign_id=campaign_id or 0,
                            outbox_id=outbox_id,
                            event_type="hard_bounce",
                            detail=diag_code,
                        )
                    )
                    processed_count += 1
                    # Evaluate circuit breaker if campaign_id is available
                    if campaign_id:
                        DeliverabilityCircuitBreaker.evaluate_campaign(db, campaign_id=campaign_id)
                else:
                    # Transient Soft Bounce: do NOT suppress globally
                    db.add(
                        CampaignEvent(
                            event_key=event_key,
                            campaign_id=campaign_id or 0,
                            outbox_id=outbox_id,
                            event_type="soft_bounce",
                            detail=diag_code,
                        )
                    )
                    if outbox_id is not None:
                        outbox = db.get(OutboxMessage, outbox_id)
                        if outbox and outbox.status in (OutboxStatus.PENDING, OutboxStatus.ACCEPTED):
                            outbox.attempts += 1
                            if outbox.attempts < 3:
                                outbox.status = OutboxStatus.RETRY
                                outbox.last_error = diag_code or "transient_bounce"
                            else:
                                outbox.status = OutboxStatus.FAILED
                                outbox.last_error = diag_code or "max_retries_exceeded"
                    processed_count += 1

        # B. Complaint Event (Spam Feedback Loop)
        elif event_type == "Complaint":
            complaint_info = message.get("complaint", {})
            complained_recipients = complaint_info.get("complainedRecipients", [])
            recipient_emails = [
                r.get("emailAddress") for r in complained_recipients if r.get("emailAddress")
            ]
            if not recipient_emails and mail.get("destination"):
                recipient_emails = mail["destination"]

            feedback_type = complaint_info.get("complaintFeedbackType", "abuse")
            detail_str = f"Spam complaint: {feedback_type}"

            for email in recipient_emails:
                event_key = f"sns:complaint:{sns_message_id}:{email}"
                existing_event = db.scalar(
                    select(CampaignEvent.id).where(CampaignEvent.event_key == event_key)
                )
                if existing_event is not None:
                    continue

                norm_email = upsert_global_suppression(
                    db,
                    email=email,
                    reason=SuppressionReason.COMPLAINT,
                    source="aws_sns",
                    detail=detail_str,
                )
                synchronize_recipient_state(
                    db,
                    email_normalized=norm_email,
                    new_status="complained",
                    campaign_id=campaign_id,
                    outbox_id=outbox_id,
                    error_detail=detail_str,
                )
                db.add(
                    CampaignEvent(
                        event_key=event_key,
                        campaign_id=campaign_id or 0,
                        outbox_id=outbox_id,
                        event_type="complaint",
                        detail=detail_str,
                    )
                )
                processed_count += 1
                if campaign_id:
                    DeliverabilityCircuitBreaker.evaluate_campaign(db, campaign_id=campaign_id)

        # C. Delivery Event
        elif event_type == "Delivery":
            delivery_info = message.get("delivery", {})
            recipient_emails = delivery_info.get("recipients", mail.get("destination", []))
            for email in recipient_emails:
                event_key = f"sns:delivery:{sns_message_id}:{email}"
                existing_event = db.scalar(
                    select(CampaignEvent.id).where(CampaignEvent.event_key == event_key)
                )
                if existing_event is not None:
                    continue

                try:
                    norm_email = normalize_email(email)
                except ValueError:
                    norm_email = email.strip().lower()

                synchronize_recipient_state(
                    db,
                    email_normalized=norm_email,
                    new_status="delivered",
                    campaign_id=campaign_id,
                    outbox_id=outbox_id,
                )
                db.add(
                    CampaignEvent(
                        event_key=event_key,
                        campaign_id=campaign_id or 0,
                        outbox_id=outbox_id,
                        event_type="delivered",
                        detail="SES delivery successful",
                    )
                )
                processed_count += 1

        db.commit()
        audit(
            db,
            "sns.notification_processed",
            detail=f"event_type={event_type};campaign_id={campaign_id};processed={processed_count}",
        )
        return {
            "status": "processed",
            "event_type": event_type,
            "recipients_processed": processed_count,
        }

    return {"status": "ignored", "reason": f"unhandled_message_type_{msg_type}"}


@router.post("/aws-sns")
async def aws_sns_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ingests AWS SNS notifications for Amazon SES events (Bounce, Complaint, Delivery).

    Performs strict SSRF defense, dual-mode RSA signature verification, and sub-second suppression sync.
    """
    raw_body = await request.body()
    return process_sns_notification(raw_body, db)
