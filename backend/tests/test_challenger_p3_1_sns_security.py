from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import AuditEvent
from app.main import app
from app.webhooks.sns_router import process_sns_notification
from app.webhooks.sns_verifier import (
    build_sns_canonical_bytes,
    clear_sns_certificate_cache,
    register_sns_certificate,
    validate_signing_cert_url,
    validate_subscribe_url,
    verify_sns_signature,
)
from tests._test_rsa_keys import CERT_PEM, D, E, N

TEST_CERT_URL_A = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-mock.pem"
TEST_CERT_URL_B = "https://sns.us-west-2.amazonaws.com/SimpleNotificationService-alt.pem"

# Distinct 2048-bit RSA Key B for Key-Confusion / Impersonation attacks
# (Different modulus and private exponent)
N_B = N + 2  # Different modulus
D_B = D + 2


@pytest.fixture(autouse=True)
def setup_test_certificates():
    clear_sns_certificate_cache()
    register_sns_certificate(TEST_CERT_URL_A, CERT_PEM)
    yield
    clear_sns_certificate_cache()


def sign_mock_sns_payload(
    payload: dict[str, Any],
    key_d: int = D,
    key_n: int = N,
    cert_url: str = TEST_CERT_URL_A,
) -> dict[str, Any]:
    """Cryptographically signs an SNS payload with RSA PKCS#1 v1.5."""
    canonical = build_sns_canonical_bytes(payload)
    k = (key_n.bit_length() + 7) // 8
    sig_ver = str(payload.get("SignatureVersion", "2"))
    if sig_ver == "2":
        digest_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
        h = hashlib.sha256(canonical).digest()
    else:
        digest_prefix = bytes.fromhex("3021300906052b0e03021a05000414")
        h = hashlib.sha1(canonical).digest()
    t = digest_prefix + h
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(t)) + b"\x00" + t
    sig_int = pow(int.from_bytes(em, "big"), key_d, key_n)
    sig_bytes = sig_int.to_bytes(k, "big")
    payload["Signature"] = base64.b64encode(sig_bytes).decode("ascii")
    payload["SigningCertURL"] = cert_url
    return payload


# ==============================================================================
# Challenge 1: Adversarial SSRF Attacks on SigningCertURL
# ==============================================================================


@pytest.mark.parametrize(
    "malicious_url, expected_error_fragment",
    [
        # 1. Domain suffix spoofing (attacker-controlled subdomain of evil domain)
        ("https://sns.us-east-1.amazonaws.com.attacker.com/cert.pem", "Untrusted certificate host"),
        ("https://sns.us-east-1.amazonaws.com.evil.org/cert.pem", "Untrusted certificate host"),
        ("https://sns.us-east-1.amazonaws.com-evil.com/cert.pem", "Untrusted certificate host"),
        # 2. Domain prefix spoofing / lookalike
        ("https://evil-sns.us-east-1.amazonaws.com/cert.pem", "Untrusted certificate host"),
        ("https://fake-sns.us-east-1.amazonaws.com/cert.pem", "Untrusted certificate host"),
        ("https://not-sns.us-east-1.amazonaws.com/cert.pem", "Untrusted certificate host"),
        # 3. Second-level domain substitution & missing sns prefix
        ("https://sns.us-east-1.evilaws.com/cert.pem", "Untrusted certificate host"),
        ("https://sns.us-east-1.amazon.com/cert.pem", "Untrusted certificate host"),
        ("https://attacker.amazonaws.com/cert.pem", "Untrusted certificate host"),
        ("https://sns.attacker.com/cert.pem", "Untrusted certificate host"),
        ("https://sns.amazonaws.com/cert.pem", "Untrusted certificate host"),
        # 4. Insecure HTTP scheme
        ("http://sns.us-east-1.amazonaws.com/cert.pem", "Insecure certificate scheme"),
        ("http://sns.us-west-2.amazonaws.com/cert.pem", "Insecure certificate scheme"),
        # 5. Dangerous URI schemes
        ("ftp://sns.us-east-1.amazonaws.com/cert.pem", "Insecure certificate scheme"),
        ("file:///etc/passwd", "Insecure certificate scheme"),
        ("gopher://127.0.0.1:25/", "Insecure certificate scheme"),
        ("data:text/plain;base64,abc", "Insecure certificate scheme"),
        ("javascript:alert(1)", "Insecure certificate scheme"),
        # 6. Intranet / Cloud Metadata IP addresses (AWS IMDSv1/v2, localhost, private RFC1918)
        ("http://169.254.169.254/latest/meta-data/", "Insecure certificate scheme"),
        ("https://169.254.169.254/latest/meta-data/", "Untrusted certificate host"),
        ("https://169.254.169.254/cert.pem", "Untrusted certificate host"),
        ("http://127.0.0.1/cert.pem", "Insecure certificate scheme"),
        ("https://127.0.0.1/cert.pem", "Untrusted certificate host"),
        ("http://localhost/cert.pem", "Insecure certificate scheme"),
        ("https://localhost/cert.pem", "Untrusted certificate host"),
        ("https://10.0.0.1/cert.pem", "Untrusted certificate host"),
        ("https://172.16.0.1/cert.pem", "Untrusted certificate host"),
        ("https://192.168.1.1/cert.pem", "Untrusted certificate host"),
        # 7. Non-standard ports
        ("https://sns.us-east-1.amazonaws.com:80/cert.pem", "Invalid certificate port"),
        ("https://sns.us-east-1.amazonaws.com:8080/cert.pem", "Invalid certificate port"),
        ("https://sns.us-east-1.amazonaws.com:8443/cert.pem", "Invalid certificate port"),
        ("https://sns.us-east-1.amazonaws.com:22/cert.pem", "Invalid certificate port"),
        # 8. Path traversal and extension tampering
        ("https://sns.us-east-1.amazonaws.com/cert.txt", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/cert.cer", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/cert.crt", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/cert.pem.txt", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/foo/../cert.pem", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/../cert.pem", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/cert.pem/..", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com/", "Invalid certificate path"),
        ("https://sns.us-east-1.amazonaws.com", "Invalid certificate path"),
    ],
)
def test_adversarial_ssrf_signing_cert_url(malicious_url, expected_error_fragment):
    """Verifies that all SSRF and spoofing payloads targeting SigningCertURL are strictly rejected."""
    with pytest.raises(ValueError, match=expected_error_fragment):
        validate_signing_cert_url(malicious_url)


@pytest.mark.parametrize(
    "malicious_url",
    [
        "https://sns.us-east-1.amazonaws.com.evil.com/cert.pem",
        "http://sns.us-east-1.amazonaws.com/cert.pem",
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/cert.pem",
        "https://sns.us-east-1.amazonaws.com:8080/cert.pem",
        "https://sns.us-east-1.amazonaws.com/foo/../cert.pem",
        "https://attacker.com/cert.pem",
    ],
)
def test_webhook_endpoint_strictly_returns_400_on_malicious_cert_url(db, malicious_url):
    """Empirically confirms that the webhook endpoint returns HTTP 400 Bad Request (not 500 or 403) and logs rejection audit."""
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": json.dumps({"eventType": "Delivery"}),
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": malicious_url,
        "Signature": base64.b64encode(b"dummy").decode(),
    }
    raw = json.dumps(payload).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(raw, db)

    assert exc_info.value.status_code == 400
    assert "certificate" in exc_info.value.detail.lower()

    # Verify security audit event was written
    audit_ev = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "webhooks.rejected",
            AuditEvent.detail.like("invalid_cert_url:%"),
        )
    )
    assert audit_ev is not None


# ==============================================================================
# Challenge 2: Adversarial SSRF Attacks on SubscribeURL
# ==============================================================================


@pytest.mark.parametrize(
    "malicious_subscribe_url, expected_error_fragment",
    [
        # Insecure HTTP
        ("http://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription", "Insecure subscribe scheme"),
        # Attacker external hosts
        ("https://evil.com/confirm", "Untrusted subscribe host"),
        ("https://sns.us-east-1.amazonaws.com.attacker.com/confirm", "Untrusted subscribe host"),
        ("https://attacker.amazonaws.com/confirm", "Untrusted subscribe host"),
        # Cloud metadata / IMDS
        ("http://169.254.169.254/latest/meta-data/", "Insecure subscribe scheme"),
        ("https://169.254.169.254/confirm", "Untrusted subscribe host"),
        # Local loopback / intranet
        ("http://127.0.0.1:8000/confirm", "Insecure subscribe scheme"),
        ("https://localhost:8443/confirm", "Invalid subscribe port"),
        ("https://10.0.0.1/confirm", "Untrusted subscribe host"),
        # Non-standard ports
        ("https://sns.us-east-1.amazonaws.com:8080/confirm", "Invalid subscribe port"),
        ("https://sns.us-east-1.amazonaws.com:80/confirm", "Invalid subscribe port"),
        ("https://sns.us-east-1.amazonaws.com:22/confirm", "Invalid subscribe port"),
    ],
)
def test_adversarial_ssrf_subscribe_url(malicious_subscribe_url, expected_error_fragment):
    """Verifies that all SSRF and intranet payloads targeting SubscribeURL are strictly rejected."""
    with pytest.raises(ValueError, match=expected_error_fragment):
        validate_subscribe_url(malicious_subscribe_url)


def test_valid_aws_sns_urls_pass_validation():
    """Confirms all legitimate regional AWS SNS endpoints and ports pass validation."""
    valid_cert_urls = [
        "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-0123.pem",
        "https://sns.us-west-2.amazonaws.com/SimpleNotificationService-4567.pem",
        "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-8901.pem",
        "https://sns.eu-central-1.amazonaws.com/SimpleNotificationService-abcd.pem",
        "https://sns.ap-southeast-1.amazonaws.com/SimpleNotificationService-ef01.pem",
        "https://sns.ap-northeast-1.amazonaws.com/SimpleNotificationService-2345.pem",
        "https://sns.cn-north-1.amazonaws.com.cn/SimpleNotificationService-china.pem",
        "https://sns.us-east-1.amazonaws.com:443/SimpleNotificationService-mock.pem",
    ]
    for url in valid_cert_urls:
        validate_signing_cert_url(url)

    valid_sub_urls = [
        "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=abc",
        "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=def",
        "https://sns.cn-north-1.amazonaws.com.cn/?Action=ConfirmSubscription&Token=xyz",
        "https://sns.us-east-1.amazonaws.com:443/?Action=ConfirmSubscription&Token=123",
    ]
    for url in valid_sub_urls:
        validate_subscribe_url(url)


# ==============================================================================
# Challenge 3: Cryptographic Signature Engine & Tampering Attacks
# ==============================================================================


def test_crypto_tampered_message_fails_and_returns_403(db):
    """Tests that modifying even one character of the Message payload causes signature verification to fail."""
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Subject": "SES Event",
        "Message": json.dumps({"eventType": "Bounce", "bounce": {"bounceType": "Permanent"}}),
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)

    # Attack 1: Alter bounce type to evade suppression
    tampered_1 = dict(signed)
    tampered_1["Message"] = json.dumps({"eventType": "Bounce", "bounce": {"bounceType": "Transient"}})
    assert verify_sns_signature(tampered_1) is False

    # Attack 2: Append single space
    tampered_2 = dict(signed)
    tampered_2["Message"] = signed["Message"] + " "
    assert verify_sns_signature(tampered_2) is False

    # Verify endpoint returns 403 Forbidden
    raw = json.dumps(tampered_1).encode("utf-8")
    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(raw, db)
    assert exc_info.value.status_code == 403
    assert "signature" in exc_info.value.detail.lower()


def test_crypto_tampered_subject_and_timestamp():
    """Tests tampering with Subject and Timestamp fields."""
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Subject": "SES Notification",
        "Message": "message body",
        "Timestamp": "2026-11-15T12:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)

    # Tamper Subject
    tampered_subj = dict(signed)
    tampered_subj["Subject"] = "Forged Subject"
    assert verify_sns_signature(tampered_subj) is False

    # Strip Subject
    stripped_subj = dict(signed)
    del stripped_subj["Subject"]
    assert verify_sns_signature(stripped_subj) is False

    # Tamper Timestamp
    tampered_ts = dict(signed)
    tampered_ts["Timestamp"] = "2026-11-15T12:00:00.001Z"
    assert verify_sns_signature(tampered_ts) is False


def test_crypto_tampered_signature_bytes():
    """Tests various corrupted, truncated, zeroed, and out-of-range signature bytes."""
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "hello",
        "Timestamp": "2026-11-15T12:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)
    valid_sig_bytes = base64.b64decode(signed["Signature"])

    # 1. Flip a single bit in valid signature
    flipped_bytes = bytearray(valid_sig_bytes)
    flipped_bytes[10] ^= 0x01
    bad_sig = dict(signed)
    bad_sig["Signature"] = base64.b64encode(flipped_bytes).decode()
    assert verify_sns_signature(bad_sig) is False

    # 2. Truncated signature (< 256 bytes)
    bad_sig["Signature"] = base64.b64encode(valid_sig_bytes[:128]).decode()
    assert verify_sns_signature(bad_sig) is False

    # 3. Oversized signature (> 256 bytes)
    bad_sig["Signature"] = base64.b64encode(valid_sig_bytes + b"\x00").decode()
    assert verify_sns_signature(bad_sig) is False

    # 4. All zero bytes
    bad_sig["Signature"] = base64.b64encode(b"\x00" * 256).decode()
    assert verify_sns_signature(bad_sig) is False

    # 5. All 0xFF bytes
    bad_sig["Signature"] = base64.b64encode(b"\xff" * 256).decode()
    assert verify_sns_signature(bad_sig) is False

    # 6. Invalid Base64 string
    bad_sig["Signature"] = "!@#$%^&*()_+"
    assert verify_sns_signature(bad_sig) is False

    # 7. Empty signature
    bad_sig["Signature"] = ""
    assert verify_sns_signature(bad_sig) is False


def test_crypto_signature_version_1_sha1_support_and_tampering():
    """Tests that SignatureVersion '1' (SHA1withRSA) is supported and strictly rejects tampering."""
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "test sha1 payload",
        "Timestamp": "2026-11-15T12:00:00.000Z",
        "SignatureVersion": "1",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed_sha1 = sign_mock_sns_payload(payload)
    assert verify_sns_signature(signed_sha1) is True

    # Tampering under SHA1 must fail
    tampered_sha1 = dict(signed_sha1)
    tampered_sha1["Message"] = "tampered sha1 payload"
    assert verify_sns_signature(tampered_sha1) is False


def test_crypto_key_confusion_attack():
    """Attack: Sign with Key A, but claim certificate belongs to Key B.

    Ensures that mismatched keys strictly fail signature verification.
    """
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "critical event",
        "Timestamp": "2026-11-15T12:00:00.000Z",
        "SignatureVersion": "2",
    }
    # Sign with Key A
    signed = sign_mock_sns_payload(payload, key_d=D, key_n=N, cert_url=TEST_CERT_URL_A)

    # Register Key B in cache under an allowed URL
    from app.webhooks.sns_verifier import _CERT_CACHE
    _CERT_CACHE[TEST_CERT_URL_B] = (N_B, E)

    # Attacker sets SigningCertURL to Key B
    signed["SigningCertURL"] = TEST_CERT_URL_B
    assert verify_sns_signature(signed) is False


def test_crypto_canonical_bytes_ordering_and_field_injection():
    """Verifies that canonical string generation strictly follows AWS specification and ignores unlisted fields."""
    payload = {
        "Type": "Notification",
        "MessageId": "msg-canonical",
        "TopicArn": "arn:aws:sns:us-east-1:123:test",
        "Message": "body",
        "Timestamp": "2026-11-15T12:00:00Z",
        "InjectedEvilField": "evil_payload",
    }
    canonical = build_sns_canonical_bytes(payload).decode("utf-8")
    assert "InjectedEvilField" not in canonical
    assert canonical == "Message\nbody\nMessageId\nmsg-canonical\nTimestamp\n2026-11-15T12:00:00Z\nTopicArn\narn:aws:sns:us-east-1:123:test\nType\nNotification\n"


# ==============================================================================
# Challenge 4: SubscriptionConfirmation Auto-Confirm Safety
# ==============================================================================


def test_subscription_confirmation_auto_confirm_safety_mock_mode(db):
    """Tests SubscriptionConfirmation auto-confirms safely in mock mode without network calls."""
    payload = {
        "Type": "SubscriptionConfirmation",
        "MessageId": str(uuid.uuid4()),
        "Token": "sub-token-12345",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "You have chosen to subscribe to topic.",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=sub-token-12345",
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)
    raw = json.dumps(signed).encode("utf-8")

    result = process_sns_notification(raw, db)
    assert result["status"] == "confirmed"
    assert result["mode"] == "mock"
    assert result["topic_arn"] == payload["TopicArn"]

    audit_ev = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "sns.subscription_confirmed",
            AuditEvent.detail.like("%mode=mock%"),
        )
    )
    assert audit_ev is not None


def test_subscription_confirmation_rejects_ssrf_even_when_signed(db):
    """Attack: Attacker creates a signed SubscriptionConfirmation with a malicious internal SubscribeURL.

    Even if signature were valid, the system must strictly reject with 400 Bad Request before attempting outbound requests.
    """
    payload = {
        "Type": "SubscriptionConfirmation",
        "MessageId": str(uuid.uuid4()),
        "Token": "sub-token-evil",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "Subscription message",
        "SubscribeURL": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)
    raw = json.dumps(signed).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(raw, db)

    assert exc_info.value.status_code == 400
    assert "subscribe scheme" in exc_info.value.detail.lower()


def test_subscription_confirmation_tampered_token_returns_403(db):
    """Attack: Tampering Token in SubscriptionConfirmation fails signature check with 403."""
    payload = {
        "Type": "SubscriptionConfirmation",
        "MessageId": str(uuid.uuid4()),
        "Token": "valid-token",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": "Subscribe message",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)
    signed["Token"] = "tampered-token"
    raw = json.dumps(signed).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(raw, db)

    assert exc_info.value.status_code == 403


# ==============================================================================
# Challenge 5: Full HTTP Route & Operator Access Boundary
# ==============================================================================


# ==============================================================================
# Challenge 5: Full Webhook Processor Rejections & Operator Access Boundary
# ==============================================================================


def test_webhook_processor_rejections_and_public_boundary(db):
    """Tests the synchronous webhook processor edge cases and confirms route boundary registration."""
    # 1. Non-JSON payload returns 400
    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(b"not a valid json payload", db)
    assert exc_info.value.status_code == 400
    assert "Invalid JSON payload" in exc_info.value.detail

    # 2. Missing Type returns 400
    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(json.dumps({"Message": "hello"}).encode(), db)
    assert exc_info.value.status_code == 400
    assert "Missing SNS message Type" in exc_info.value.detail

    # 3. Missing SigningCertURL returns 400
    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(json.dumps({"Type": "Notification", "Message": "test"}).encode(), db)
    assert exc_info.value.status_code == 400
    assert "SigningCertURL" in exc_info.value.detail

    # 4. Null SigningCertURL returns 400
    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(
            json.dumps({"Type": "Notification", "SigningCertURL": None, "Message": "test"}).encode(),
            db,
        )
    assert exc_info.value.status_code == 400
    assert "SigningCertURL" in exc_info.value.detail

    # 5. Malicious SigningCertURL returns 400
    with pytest.raises(HTTPException) as exc_info:
        process_sns_notification(
            json.dumps({
                "Type": "Notification",
                "SigningCertURL": "http://169.254.169.254/",
                "Message": "test",
            }).encode(),
            db,
        )
    assert exc_info.value.status_code == 400
    assert "certificate" in exc_info.value.detail.lower()

    # 6. Valid signed delivery payload succeeds
    payload = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bms-events",
        "Message": json.dumps({"eventType": "Delivery", "mail": {"destination": ["client_test@example.com"]}}),
        "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "SignatureVersion": "2",
        "SigningCertURL": TEST_CERT_URL_A,
    }
    signed = sign_mock_sns_payload(payload)
    resp = process_sns_notification(json.dumps(signed).encode(), db)
    assert resp["status"] == "processed"
    assert resp["event_type"] == "Delivery"

    # 7. Confirm /webhooks/aws-sns route is publicly accessible and registered
    route_paths = [getattr(route, "path", None) for route in app.routes]
    assert "/webhooks/aws-sns" in route_paths


