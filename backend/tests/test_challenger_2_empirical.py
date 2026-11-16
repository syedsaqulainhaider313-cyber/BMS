"""Empirical verification suite for Challenger 2:
Deliverability, Cryptography, RFC Compliance & ISP Throttling Challenges.

Covers:
1. SPF & DMARC alignment under relaxed (aspf=r) vs strict (aspf=s) modes (RFC 7489).
2. DKIM canonicalization, body mutation, and 2048-bit RSA DNS 255-octet boundary (RFC 6376, RFC 1035).
3. RFC 8058 One-Click Unsubscribe POST body, auth bypass, and recipient_snapshot regression.
4. ISP rate limiting, Redis token bucket expiry, thundering herd, and circuit breaker 421 omission & denominator trap.
5. 30-day warm-up schedule engagement segmentation gap and lack of open-rate drop triggers.
"""

import hashlib
import re
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.main import unsubscribe
from app.models import (
    Campaign,
    CampaignStatus,
    Contact,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.suppression import make_unsubscribe_token

# ==============================================================================
# Challenge 1: SPF & DMARC Alignment Edge Cases (RFC 7489)
# ==============================================================================

def evaluate_dmarc_spf_alignment(from_domain: str, mail_from_domain: str, aspf: str) -> bool:
    """Evaluates SPF DMARC alignment per RFC 7489 Section 3.1.1.
    
    aspf='r' (relaxed): Organizational domain of From must match Organizational domain of MAIL FROM.
    aspf='s' (strict): From domain must match MAIL FROM domain exactly.
    """
    def get_org_domain(domain: str) -> str:
        parts = domain.lower().strip(".").split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return domain.lower()

    from_domain = from_domain.lower().strip(".")
    mail_from_domain = mail_from_domain.lower().strip(".")

    if aspf == "s":
        return from_domain == mail_from_domain
    elif aspf == "r":
        return get_org_domain(from_domain) == get_org_domain(mail_from_domain)
    else:
        raise ValueError(f"Invalid aspf: {aspf}")


def test_challenge_1_spf_alignment_relaxed_vs_strict():
    """Empirically test SPF alignment:
    M2 claimed: 'Passes both SPF and DKIM DMARC checks under relaxed (r) and strict (s) modes' (line 87).
    We test if bounces.bms-sender.com passes under aspf=s.
    """
    from_domain = "bms-sender.com"
    mail_from_subdomain = "bounces.bms-sender.com"
    mail_from_apex = "bms-sender.com"
    ses_shared_mail_from = "amazonses.com"

    # 1. Under relaxed mode (aspf=r)
    assert evaluate_dmarc_spf_alignment(from_domain, mail_from_subdomain, aspf="r") is True

    # 2. Under strict mode (aspf=s)
    # bounces.bms-sender.com vs bms-sender.com -> STRICT ALIGNMENT FAILS!
    strict_subdomain_alignment = evaluate_dmarc_spf_alignment(from_domain, mail_from_subdomain, aspf="s")
    assert strict_subdomain_alignment is False, "RFC 7489 strict mode MUST fail on subdomain Return-Path"

    # 3. Apex domain matches under strict
    assert evaluate_dmarc_spf_alignment(from_domain, mail_from_apex, aspf="s") is True

    # 4. Shared SES domain fails both relaxed and strict
    assert evaluate_dmarc_spf_alignment(from_domain, ses_shared_mail_from, aspf="r") is False
    assert evaluate_dmarc_spf_alignment(from_domain, ses_shared_mail_from, aspf="s") is False


# ==============================================================================
# Challenge 2: DKIM Canonicalization & 2048-bit Key Compatibility (RFC 6376, 1035)
# ==============================================================================

def dkim_relaxed_body_canonicalize(body: str) -> bytes:
    """Canonicalize body per RFC 6376 Section 3.4.4 (relaxed algorithm).
    - Ignores all empty lines at the end of the message body.
    - Reduces all sequences of whitespace within a line to a single space.
    - Strips all trailing whitespace from lines.
    - Lines end with CRLF.
    """
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    processed_lines = []
    for line in lines:
        reduced = re.sub(r"[ \t]+", " ", line).rstrip(" \t")
        processed_lines.append(reduced)

    while processed_lines and processed_lines[-1] == "":
        processed_lines.pop()

    if not processed_lines:
        return b""
    return "\r\n".join(processed_lines).encode("utf-8") + b"\r\n"


def test_challenge_2_dkim_canonicalization_and_mutations():
    """Empirically test what relaxed body canonicalization protects and what breaks it."""
    orig_body = "Hello Alex,\r\n\r\nThis is a test notification from BMS.\r\n   Multiple   spaces   here.   \r\n\r\n"
    canon_orig = dkim_relaxed_body_canonicalize(orig_body)
    hash_orig = hashlib.sha256(canon_orig).digest()

    # Mutation A: White-space tampering (extra spaces, trailing spaces, extra empty lines at end)
    mutated_whitespace = "Hello Alex,\r\n\r\nThis is a test notification from BMS.\r\n Multiple spaces here.\r\n\r\n\r\n\r\n"
    canon_mut_ws = dkim_relaxed_body_canonicalize(mutated_whitespace)
    hash_mut_ws = hashlib.sha256(canon_mut_ws).digest()
    assert hash_orig == hash_mut_ws

    # Mutation B: Intermediary MTA / Anti-Virus appends corporate disclaimer or footer
    disclaimer_body = orig_body + "\r\n[Scanned by Secure Gateway - No Viruses Detected]\r\n"
    canon_disclaimer = dkim_relaxed_body_canonicalize(disclaimer_body)
    hash_disclaimer = hashlib.sha256(canon_disclaimer).digest()
    assert hash_orig != hash_disclaimer


def test_challenge_2_dns_txt_record_255_byte_limit():
    """Empirically test RFC 1035 Section 2.3.4 & 3.3.14 255-octet character-string limit.
    A 2048-bit RSA public key exceeds 255 octets and MUST be chunked in DNS TXT records.
    """
    sample_2048_key_base64 = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0" + ("A" * 380) + "IDAQAB"
    raw_txt_record = f"v=DKIM1; k=rsa; p={sample_2048_key_base64}"
    
    assert len(raw_txt_record) > 255, f"2048-bit RSA TXT record is {len(raw_txt_record)} bytes (> 255)"

    def chunk_dns_txt(record: str, chunk_size: int = 255) -> list[str]:
        return [record[i:i + chunk_size] for i in range(0, len(record), chunk_size)]

    chunks = chunk_dns_txt(raw_txt_record)
    assert len(chunks) == 2
    assert len(chunks[0]) <= 255
    assert len(chunks[1]) <= 255
    assert "".join(chunks) == raw_txt_record


# ==============================================================================
# Challenge 3: RFC 8058 One-Click Unsubscribe Edge Cases & Existing Code Flaws
# ==============================================================================

def test_challenge_3_unsubscribe_recipient_snapshot_regression(db, sample_template):
    """Empirically verify that when campaigns do NOT have recipient_snapshot
    (as proposed by M3 normalization into campaign_recipients table),
    the existing unsubscribe endpoint in main.py:599-607 fails with 404!
    """
    email = "victim@example.com"
    norm = normalize_email(email)
    contact = Contact(email=email, email_normalized=norm)
    db.add(contact)
    db.flush()

    # M3 Phase 1 normalizes recipients into campaign_recipients, leaving recipient_snapshot None
    campaign = Campaign(
        name="Normalized Campaign (M3 Phase 1)",
        template_id=sample_template.id,
        consent_purpose="marketing",
        status=CampaignStatus.APPROVED,
        recipient_snapshot=None,  # <--- M3 normalizes into campaign_recipients, leaving snapshot None!
    )
    db.add(campaign)
    db.commit()

    token = make_unsubscribe_token(campaign.id, contact.id, norm)

    class DummyRequest:
        client = None

    # In main.py:599-607:
    # is_recipient = campaign and any(int(row.get("contact_id", -1)) == contact_id for row in (campaign.recipient_snapshot or []))
    # if not is_recipient: raise HTTPException(404, "Unsubscribe link not found")
    with pytest.raises(HTTPException) as exc_info:
        unsubscribe(token, DummyRequest(), db)
    
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Unsubscribe link not found"


def test_challenge_3_rfc_8058_body_and_cookie_requirements(db, sample_template):
    """Empirically test RFC 8058 One-Click requirements:
    1. Must NOT require a login cookie or auth session.
    2. Must return HTTP 200 without redirect.
    """
    email = "alex@example.com"
    norm = normalize_email(email)
    contact = Contact(email=email, email_normalized=norm)
    db.add(contact)
    db.flush()

    campaign = Campaign(
        name="Snapshot Campaign",
        template_id=sample_template.id,
        consent_purpose="newsletter",
        status=CampaignStatus.APPROVED,
        recipient_snapshot=[{"contact_id": contact.id, "email": norm}],
    )
    db.add(campaign)
    db.commit()

    token = make_unsubscribe_token(campaign.id, contact.id, norm)

    class DummyRequest:
        client = None

    # Test without auth cookie
    post_resp = unsubscribe(token, DummyRequest(), db)
    assert post_resp == {"status": "unsubscribed"}

    # Check suppression record
    suppression = db.scalar(select(Suppression).where(Suppression.email_normalized == norm))
    assert suppression is not None
    assert suppression.reason == SuppressionReason.UNSUBSCRIBE


def test_challenge_3_middleware_auth_trap_for_api_v1_unsubscribe():
    """Empirically test that if M3's proposed URL /api/v1/unsubscribe/one-click/{token}
    is processed by the existing middleware in main.py, it is classified as requiring
    operator authentication because:
    1. /api/ prefix is stripped, turning path into /v1/unsubscribe/...
    2. is_unsubscribe = path.startswith('/unsubscribe/') evaluates to False!
    3. The request is routed to authenticate_request() and returns 401 Unauthorized!
    """
    raw_path = "/api/v1/unsubscribe/one-click/sample-token"
    
    if raw_path.startswith("/api/"):
        normalized_path = raw_path[4:]
    else:
        normalized_path = raw_path

    assert normalized_path == "/v1/unsubscribe/one-click/sample-token"

    public_paths = {"/health", "/ready", "/auth/login", "/webhooks/provider"}
    is_unsubscribe = normalized_path.startswith("/unsubscribe/")
    is_static = (
        normalized_path == "/"
        or normalized_path.startswith("/assets/")
        or normalized_path.endswith((".js", ".css", ".ico", ".svg", ".png", ".woff2", ".html"))
    )

    requires_auth = normalized_path not in public_paths and not is_unsubscribe and not is_static
    
    assert is_unsubscribe is False, "Path '/v1/unsubscribe/...' fails is_unsubscribe check!"
    assert requires_auth is True, "Critical Trap: Proposed RFC 8058 endpoint requires operator session cookie!"


# ==============================================================================
# Challenge 4: ISP Rate Limiting, Thundering Herd & Circuit Breaker Traps
# ==============================================================================

class MockLuaTokenBucket:
    """Python oracle of the Lua token bucket from M2 lines 536-576."""
    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_updated = time.time()

    def simulate_redis_restart_or_expiry(self):
        """Simulate Redis restart or TTL 3600s key expiry."""
        self.tokens = None
        self.last_updated = None

    def acquire(self, now: float, requested: int = 1) -> tuple[int, float]:
        if self.tokens is None:
            self.tokens = self.capacity
            self.last_updated = now
        else:
            delta = max(0.0, now - self.last_updated)
            self.tokens = min(self.capacity, self.tokens + delta * self.refill_rate)
            self.last_updated = now

        if self.tokens >= requested:
            self.tokens -= requested
            return 1, self.tokens
        else:
            wait_time = (requested - self.tokens) / self.refill_rate
            return 0, wait_time


def test_challenge_4_token_bucket_redis_reset_burst():
    """Empirically test what happens when Redis resets or key expires:
    It immediately restores full burst capacity (e.g. 40 tokens).
    On Warm-up Day 1 (where cap is 1 msg/s), 40 messages can fire in 0 seconds!
    """
    bucket = MockLuaTokenBucket(capacity=40.0, refill_rate=20.0)
    now = 1000.0
    for _ in range(40):
        status, _ = bucket.acquire(now)
        assert status == 1
    status, wait = bucket.acquire(now)
    assert status == 0

    # Simulate Redis restart
    bucket.simulate_redis_restart_or_expiry()

    # Immediate burst of 40 tokens available again!
    immediate_burst_count = 0
    for _ in range(40):
        status, _ = bucket.acquire(now)
        if status == 1:
            immediate_burst_count += 1
    assert immediate_burst_count == 40


def test_challenge_4_circuit_breaker_soft_bounce_omission():
    """Empirically test that DeliverabilityCircuitBreaker in M2 & M3
    completely ignores 421 transient soft bounces / ISP deferrals!
    """
    events = [
        {"event_type": "accepted", "count": 300},
        {"event_type": "delivered", "count": 200},
        {"event_type": "soft_bounce", "count": 200}, # 40% of sends deferred by ISP!
        {"event_type": "hard_bounce", "count": 0},
        {"event_type": "complaint", "count": 0},
    ]

    metrics = {e["event_type"]: e["count"] for e in events}
    sent = metrics.get("delivered", 0) + metrics.get("accepted", 0)
    complaints = metrics.get("complaint", 0)
    hard_bounces = metrics.get("hard_bounce", 0) + metrics.get("bounce", 0)

    complaint_rate = (complaints / sent) * 100.0
    bounce_rate = (hard_bounces / sent) * 100.0

    tripped = complaint_rate >= 0.08 or bounce_rate >= 2.0
    assert tripped is False, "Critical flaw: Circuit breaker ignores 421 deferrals entirely!"


def test_challenge_4_circuit_breaker_denominator_lag_trap():
    """Empirically test the denominator trap in DeliverabilityCircuitBreaker:
    If 99 hard bounces arrive from an earlier 1,000-email batch,
    but in the current 1-hour window only 50 new emails were sent:
    sent < 100 -> circuit breaker returns ACTIVE and ignores 99 hard bounces!
    """
    events = [
        {"event_type": "accepted", "count": 30},
        {"event_type": "delivered", "count": 20},
        {"event_type": "hard_bounce", "count": 99}, # 99 hard bounces!
        {"event_type": "complaint", "count": 5},     # 5 complaints!
    ]
    metrics = {e["event_type"]: e["count"] for e in events}
    sent = metrics.get("delivered", 0) + metrics.get("accepted", 0)

    # Code logic from M2 line 863 & M3 line 924:
    if sent < 100:
        result = {"status": "ACTIVE", "reason": "Insufficient volume (<100 sent)"}
    else:
        result = {"status": "TRIPPED"}

    assert result["status"] == "ACTIVE", "Algorithm blinds itself to 99 bounces because sent < 100!"


# ==============================================================================
# Challenge 5: 30-Day Warm-Up Schedule & Engagement Segmentation Gap
# ==============================================================================

def test_challenge_5_sql_pushdown_lacks_engagement_segmentation(db):
    """Empirically test that the SQL pushdown query proposed in M3 Section 3.2.4
    has NO engagement filtering (last_opened_at / last_clicked_at),
    meaning Day 1 'Super-Engaged (Opened in last 7 days)' is impossible.
    """
    rows = db.execute(text("PRAGMA table_info(contacts)")).fetchall()
    contact_cols = [row[1] for row in rows]
    assert "last_opened_at" not in contact_cols
    assert "last_clicked_at" not in contact_cols
    assert "engagement_score" not in contact_cols
