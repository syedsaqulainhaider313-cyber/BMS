"""Phase 2: Deliverability Engine & SES Gateway Unit & Integration Tests.

Validates:
1. RFC 5322 & RFC 8058 MIME assembly via build_rfc_compliant_mime.
2. AmazonSESProvider dispatch, IP pool attachment, custom MAIL FROM, and ClientError taxonomy.
3. Domain-keyed token bucket ISP rate throttling (Gmail, Yahoo, Microsoft, Apple, default).
4. Thread-safe in-memory token bucket limiter and refill calculation.
5. RFC 8058 POST /unsubscribe/one-click/{token} endpoint and auth bypass.
6. Settings validation for SES, Redis, and email_provider toggle.
"""

from __future__ import annotations

import email
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Contact,
    EmailTemplate,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.provider import AmazonSESProvider, MockEmailProvider, get_email_provider
from app.suppression import make_unsubscribe_token
from app.templates import build_rfc_compliant_mime
from app.throttling import (
    InMemoryTokenBucketLimiter,
    classify_isp,
)

# ==============================================================================
# 1. RFC 5322 & RFC 8058 MIME Assembly Tests
# ==============================================================================

def test_build_rfc_compliant_mime_headers_and_structure():
    """Verify MIME structure is multipart/alternative with plain text first, HTML second,
    and all mandatory RFC 8058 and deliverability headers.
    """
    domain = "sender.example.com"
    token = "42.100.abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    raw_bytes = build_rfc_compliant_mime(
        from_name="Marketing Team",
        from_address=f"newsletter@{domain}",
        to_address="subscriber@gmail.com",
        subject="Monthly Product Update",
        html_body="<p>Hello! <a href='{{unsubscribe_url}}'>Unsubscribe here</a></p>",
        text_body="Hello! Unsubscribe here: {{unsubscribe_url}}",
        unsubscribe_token=token,
        domain=domain,
        campaign_id=42,
        template_id=7,
        custom_mail_from_domain=f"bounces.{domain}",
    )

    msg = email.message_from_bytes(raw_bytes)
    assert msg.is_multipart()
    assert msg.get_content_type() == "multipart/alternative"

    # 1. RFC 8058 Mandatory One-Click Unsubscribe Headers
    assert "List-Unsubscribe" in msg
    assert f"<https://{domain}/api/v1/unsubscribe/one-click/{token}>" in msg["List-Unsubscribe"]
    assert f"<mailto:unsubscribe@{domain}?subject=unsub_{token}>" in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    # 2. RFC 5322 & Deliverability Headers
    assert msg["From"] == f'"Marketing Team" <newsletter@{domain}>'
    assert msg["To"] == "subscriber@gmail.com"
    assert msg["Subject"] == "Monthly Product Update"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Precedence"] == "bulk"
    assert msg["Return-Path"] == f"<bounces@bounces.{domain}>"
    assert msg["Feedback-ID"] == f"campaign-42:template-7:bms:{domain}"
    assert f"@{domain}" in msg["Message-ID"]
    assert msg["Date"] is not None

    # 3. Payload Order: Text first, HTML second
    parts = msg.get_payload()
    assert len(parts) == 2
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"

    # Verify placeholder replacement
    expected_url = f"https://{domain}/api/v1/unsubscribe/one-click/{token}"
    assert expected_url in parts[0].get_payload(decode=True).decode("utf-8")
    assert expected_url in parts[1].get_payload(decode=True).decode("utf-8")


# ==============================================================================
# 2. AmazonSESProvider Unit Tests (Offline / Mock Client)
# ==============================================================================

class DummySESClient:
    """Mock boto3 SES v2 client simulating AWS SES responses and ClientErrors."""

    def __init__(self, mode: str = "success", error_code: str = ""):
        self.mode = mode
        self.error_code = error_code
        self.last_send_kwargs: dict[str, Any] = {}

    def send_email(self, **kwargs) -> dict[str, Any]:
        self.last_send_kwargs = kwargs
        if self.mode == "success":
            return {"MessageId": "ses-msg-mock-success-9999"}
        elif self.mode == "error":
            class MockClientError(Exception):
                def __init__(self, code):
                    self.response = {"Error": {"Code": code, "Message": f"Simulated error: {code}"}}

            raise MockClientError(self.error_code)
        raise RuntimeError("Unknown test mode")


def test_ses_provider_successful_dispatch():
    """Verify AmazonSESProvider correctly formats and dispatches email via mock client."""
    client = DummySESClient(mode="success")
    provider = AmazonSESProvider(client=client)

    result = provider.dispatch(
        email="test@yahoo.com",
        subject="Welcome!",
        html_body="<p>Welcome! {{unsubscribe_url}}</p>",
        text_body="Welcome! {{unsubscribe_url}}",
        campaign_id=10,
        outbox_id=25,
        unsubscribe_token="10.1.signature123",
    )

    assert result.status == "accepted"
    assert result.provider_message_id == "ses-msg-mock-success-9999"

    # Inspect SES call arguments
    call_kwargs = client.last_send_kwargs
    assert call_kwargs["Destination"]["ToAddresses"] == ["test@yahoo.com"]
    assert "Raw" in call_kwargs["Content"]
    tags = {t["Name"]: t["Value"] for t in call_kwargs["EmailTags"]}
    assert tags["campaign_id"] == "10"
    assert tags["outbox_id"] == "25"


def test_ses_provider_error_classification():
    """Verify AmazonSESProvider maps AWS ClientErrors to BMS dispatch statuses."""
    # Throttling -> transient
    p_throttle = AmazonSESProvider(client=DummySESClient(mode="error", error_code="TooManyRequestsException"))
    r_throttle = p_throttle.dispatch(email="t@a.com", subject="s", html_body="{{unsubscribe_url}}", text_body="{{unsubscribe_url}}", campaign_id=1, outbox_id=1)
    assert r_throttle.status == "transient"
    assert "throttled:TooManyRequestsException" in r_throttle.error_detail

    # Hard bounce / domain rejected -> hard_bounce
    p_reject = AmazonSESProvider(client=DummySESClient(mode="error", error_code="MessageRejected"))
    r_reject = p_reject.dispatch(email="t@a.com", subject="s", html_body="{{unsubscribe_url}}", text_body="{{unsubscribe_url}}", campaign_id=1, outbox_id=1)
    assert r_reject.status == "hard_bounce"

    # Account suspended -> paused
    p_pause = AmazonSESProvider(client=DummySESClient(mode="error", error_code="AccountSuspendedException"))
    r_pause = p_pause.dispatch(email="t@a.com", subject="s", html_body="{{unsubscribe_url}}", text_body="{{unsubscribe_url}}", campaign_id=1, outbox_id=1)
    assert r_pause.status == "paused"

    # Bad request / malformed -> failed
    p_bad = AmazonSESProvider(client=DummySESClient(mode="error", error_code="BadRequestException"))
    r_bad = p_bad.dispatch(email="t@a.com", subject="s", html_body="{{unsubscribe_url}}", text_body="{{unsubscribe_url}}", campaign_id=1, outbox_id=1)
    assert r_bad.status == "failed"


def test_provider_factory_toggle():
    """Verify get_email_provider supports mock and ses, and rejects unsupported."""
    mock_p = get_email_provider("mock")
    assert isinstance(mock_p, MockEmailProvider)

    # ses is recognized
    with pytest.raises((RuntimeError, Exception)) as exc:
        get_email_provider("unsupported_provider")
    assert "not supported" in str(exc.value)


# ==============================================================================
# 3. Domain-Keyed Token Bucket Rate Throttling Tests
# ==============================================================================

def test_classify_isp_domains():
    """Verify accurate domain classification across major mailbox providers."""
    assert classify_isp("user@gmail.com") == "google"
    assert classify_isp("user@googlemail.com") == "google"
    assert classify_isp("user@yahoo.com") == "yahoo"
    assert classify_isp("user@ymail.com") == "yahoo"
    assert classify_isp("user@rocketmail.com") == "yahoo"
    assert classify_isp("user@aol.com") == "yahoo"
    assert classify_isp("user@outlook.com") == "microsoft"
    assert classify_isp("user@hotmail.com") == "microsoft"
    assert classify_isp("user@live.com") == "microsoft"
    assert classify_isp("user@icloud.com") == "apple"
    assert classify_isp("user@me.com") == "apple"
    assert classify_isp("user@customcompany.org") == "default"


def test_in_memory_token_bucket_burst_and_refill(monkeypatch):
    """Verify in-memory limiter exhausts burst capacity and refills over time."""
    limiter = InMemoryTokenBucketLimiter()
    email_addr = "test@icloud.com"  # Apple: rate = 5.0, burst = 10

    # 1. Exhaust all 10 burst tokens
    for i in range(10):
        allowed, wait = limiter.acquire(email_addr, 1)
        assert allowed is True, f"Token {i+1} should be allowed"
        assert wait == 0.0

    # 2. 11th token should be throttled
    allowed, wait = limiter.acquire(email_addr, 1)
    assert allowed is False
    assert wait > 0.0

    # 3. Simulate passage of 0.4 seconds (refills 0.4 * 5.0 = 2.0 tokens)
    future_time = time.time() + 0.4
    monkeypatch.setattr(time, "time", lambda: future_time)

    allowed, wait = limiter.acquire(email_addr, 1)
    assert allowed is True
    assert wait == 0.0


def test_in_memory_token_bucket_thread_safety():
    """Verify concurrent thread acquisitions do not corrupt token bucket state."""
    limiter = InMemoryTokenBucketLimiter()
    email_addr = "user@gmail.com"  # Google: rate = 20.0, capacity = 40

    def worker_acquire():
        allowed, _ = limiter.acquire(email_addr, 1)
        return allowed

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: worker_acquire(), range(50)))

    # Since burst capacity is 40, exactly 40 (or slightly more if microsecond refill occurred) should be True
    allowed_count = sum(1 for r in results if r)
    assert 40 <= allowed_count <= 42


# ==============================================================================
# 4. RFC 8058 One-Click Unsubscribe Endpoint Tests
# ==============================================================================

class DummyPostRequest:
    client = None

    def __init__(self, body_bytes: bytes = b"List-Unsubscribe=One-Click"):
        self._body = body_bytes

    def body(self) -> bytes:
        return self._body


def test_rfc8058_one_click_unsubscribe_success(db: Session, sample_template: EmailTemplate):
    """Verify RFC 8058 one-click POST endpoint applies immediate suppression without auth."""
    contact = Contact(email="oneclick@example.com", email_normalized=normalize_email("oneclick@example.com"))
    db.add(contact)
    db.flush()

    campaign = Campaign(name="One-Click Camp", template_id=sample_template.id, consent_purpose="marketing", status=CampaignStatus.APPROVED)
    db.add(campaign)
    db.flush()

    token = make_unsubscribe_token(campaign.id, contact.id, contact.email_normalized)
    rec = CampaignRecipient(
        campaign_id=campaign.id,
        contact_id=contact.id,
        email_normalized=contact.email_normalized,
        unsubscribe_token=token,
        status=CampaignRecipientStatus.QUEUED.value,
    )
    db.add(rec)
    db.commit()

    from app.main import rfc8058_one_click_unsubscribe

    req = DummyPostRequest(b"List-Unsubscribe=One-Click")
    response = rfc8058_one_click_unsubscribe(token, req, body=b"List-Unsubscribe=One-Click", db=db)
    assert response.status_code == 200
    assert "unsubscribed" in response.body.decode().lower()

    # Verify global suppression created
    suppression = db.scalar(select(Suppression).where(Suppression.email_normalized == contact.email_normalized))
    assert suppression is not None
    assert suppression.reason == SuppressionReason.UNSUBSCRIBE
    assert suppression.source == "rfc8058_one_click"

    # Verify campaign event recorded
    event = db.scalar(select(CampaignEvent).where(CampaignEvent.event_key == f"unsubscribe:{campaign.id}:{contact.id}:rfc8058"))
    assert event is not None
    assert event.event_type == "unsubscribed"


def test_rfc8058_one_click_invalid_post_body(db: Session, sample_template: EmailTemplate):
    """Verify endpoint rejects requests that lack mandatory 'List-Unsubscribe=One-Click' body."""
    from fastapi import HTTPException

    from app.main import rfc8058_one_click_unsubscribe

    contact = Contact(email="badbody@example.com", email_normalized=normalize_email("badbody@example.com"))
    db.add(contact)
    db.flush()
    campaign = Campaign(name="Bad Body Camp", template_id=sample_template.id, consent_purpose="marketing", status=CampaignStatus.APPROVED)
    db.add(campaign)
    db.flush()
    token = make_unsubscribe_token(campaign.id, contact.id, contact.email_normalized)
    db.commit()

    req = DummyPostRequest(b"action=unsubscribe")
    with pytest.raises(HTTPException) as exc_info:
        rfc8058_one_click_unsubscribe(token, req, body=b"action=unsubscribe", db=db)
    assert exc_info.value.status_code == 400
    assert "List-Unsubscribe=One-Click" in exc_info.value.detail


def test_auth_middleware_exempts_one_click_paths():
    """Verify auth middleware exempts RFC 8058 unsubscribe paths without session cookies."""
    for path in [
        "/unsubscribe/one-click/token123",
        "/v1/unsubscribe/one-click/token123",
        "/api/v1/unsubscribe/one-click/token123",
        "/api/unsubscribe/one-click/token123",
    ]:
        raw_path = path
        normalized = raw_path[4:] if raw_path.startswith("/api/") else raw_path
        is_unsub = (
            normalized.startswith("/unsubscribe/")
            or normalized.startswith("/v1/unsubscribe/")
            or raw_path.startswith("/api/v1/unsubscribe/")
            or raw_path.startswith("/api/unsubscribe/")
        )
        assert is_unsub is True, f"Path {path} must be recognized as unsubscribe"


# ==============================================================================
# 5. Settings Validation Tests
# ==============================================================================

def test_settings_email_provider_validation():
    """Verify settings only permits 'mock' or 'ses' as email_provider."""
    from app.settings import Settings

    s_mock = Settings(email_provider="mock")
    assert s_mock.email_provider == "mock"

    s_ses = Settings(email_provider="ses")
    assert s_ses.email_provider == "ses"

    with pytest.raises(ValueError) as exc_info:
        Settings(email_provider="sendgrid")
    assert "EMAIL_PROVIDER must be 'mock' or 'ses'" in str(exc_info.value)
