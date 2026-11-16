"""Unit and integration tests for Operator TOTP MFA and PII Log Redaction."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import (
    compute_totp_code,
    generate_totp_secret,
    get_totp_uri,
    password_hash,
    verify_totp_code,
)
from app.logging_config import PIIRedactingFilter, redact_text
from app.main import LoginRequest, login
from app.models import Base
from app.settings import settings


@pytest.fixture
def mfa_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_totp_rfc6238_generation_and_verification():
    # 1. Generate standard 160-bit Base32 secret
    secret = generate_totp_secret()
    assert len(secret) >= 26

    # 2. Compute current code
    now = int(time.time())
    code = compute_totp_code(secret, for_time=now)
    assert len(code) == 6
    assert code.isdigit()

    # 3. Verify valid code
    assert verify_totp_code(secret, code) is True

    # 4. Verify invalid code rejected
    assert verify_totp_code(secret, "000000" if code != "000000" else "111111") is False
    assert verify_totp_code(secret, "invalid") is False
    assert verify_totp_code(secret, "123") is False

    # 5. Verify clock drift window (±30s)
    drift_past = compute_totp_code(secret, for_time=now - 30)
    drift_future = compute_totp_code(secret, for_time=now + 30)
    assert verify_totp_code(secret, drift_past, window=1) is True
    assert verify_totp_code(secret, drift_future, window=1) is True

    # Beyond window should fail
    far_past = compute_totp_code(secret, for_time=now - 90)
    assert verify_totp_code(secret, far_past, window=1) is False

    # 6. Authenticator URI format
    uri = get_totp_uri(secret, "operator", issuer="BMS")
    assert uri.startswith("otpauth://totp/BMS%3Aoperator?")
    assert f"secret={secret}" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_login_with_mfa_enforcement(mfa_db: Session, monkeypatch):
    test_secret = generate_totp_secret()
    test_password = "CorrectHorseBatteryStaple!23"
    test_hash = password_hash.hash(test_password)

    monkeypatch.setattr(settings, "operator_username", "mfa_operator")
    monkeypatch.setattr(settings, "operator_password_hash", test_hash)
    monkeypatch.setattr(settings, "operator_totp_secret", test_secret)

    mock_request = MagicMock()
    mock_request.client.host = "127.0.0.1"
    mock_response = MagicMock()

    # Case 1: Wrong password -> rejected immediately
    with pytest.raises(HTTPException) as exc_info:
        login(
            LoginRequest(username="mfa_operator", password="WrongPassword"),
            request=mock_request,
            response=mock_response,
            db=mfa_db,
        )
    assert exc_info.value.status_code == 401
    assert "Invalid operator credentials" in exc_info.value.detail

    # Case 2: Correct password, but TOTP code omitted -> prompts for MFA
    res = login(
        LoginRequest(username="mfa_operator", password=test_password),
        request=mock_request,
        response=mock_response,
        db=mfa_db,
    )
    assert res.get("mfa_required") is True
    assert "code required" in res.get("message", "")

    # Case 3: Correct password, invalid TOTP code -> rejected
    with pytest.raises(HTTPException) as exc_info:
        login(
            LoginRequest(username="mfa_operator", password=test_password, totp_code="999999"),
            request=mock_request,
            response=mock_response,
            db=mfa_db,
        )
    assert exc_info.value.status_code == 401
    assert "Invalid two-factor authentication code" in exc_info.value.detail

    # Case 4: Correct password and valid TOTP code -> authenticated
    valid_code = compute_totp_code(test_secret)
    res_success = login(
        LoginRequest(username="mfa_operator", password=test_password, totp_code=valid_code),
        request=mock_request,
        response=mock_response,
        db=mfa_db,
    )
    assert "csrf_token" in res_success
    assert res_success["username"] == "mfa_operator"


def test_pii_logging_redaction_filter():
    filter_instance = PIIRedactingFilter()

    # 1. Email redaction
    log_text = "Sending campaign notification to recipient user.test+filter@sub.domain.org."
    redacted = redact_text(log_text)
    assert "user.test+filter@sub.domain.org" not in redacted
    assert "[REDACTED_EMAIL]" in redacted

    # 2. Session cookie redaction
    cookie_log = "Incoming request with cookie: bms_session=abc123xyz_token_here; other=val"
    redacted_cookie = redact_text(cookie_log)
    assert "abc123xyz_token_here" not in redacted_cookie
    assert "bms_session=[REDACTED]" in redacted_cookie

    # 3. Webhook signature redaction
    sig_log = "Webhook received with X-Provider-Signature: a1b2c3d4e5f60718293048576"
    redacted_sig = redact_text(sig_log)
    assert "a1b2c3d4e5f60718293048576" not in redacted_sig
    assert "X-Provider-Signature: [REDACTED]" in redacted_sig

    # 4. Bearer authorization token redaction
    auth_log = "Header Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    redacted_auth = redact_text(auth_log)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted_auth
    assert "Authorization: [REDACTED]" in redacted_auth

    # 5. Passwords and secret values in JSON / query text
    json_log = '{"username": "operator", "password": "super-secret-password-123", "action": "login"}'
    redacted_json = redact_text(json_log)
    assert "super-secret-password-123" not in redacted_json
    assert '"password": "[REDACTED]"' in redacted_json

    # 6. HTML body redaction
    html_log = "Dispatching payload: <html><body><h1>Hello World</h1><a href='...'>Unsub</a></body></html>"
    redacted_html = redact_text(html_log)
    assert "<h1>Hello World</h1>" not in redacted_html
    assert "[REDACTED_HTML_BODY]" in redacted_html

    # 7. LogRecord filter check
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Operator logged in with email target@example.com and session bms_session=secret123",
        args=(),
        exc_info=None,
    )
    filter_instance.filter(record)
    assert "target@example.com" not in record.msg
    assert "secret123" not in record.msg
    assert "[REDACTED_EMAIL]" in record.msg
    assert "bms_session=[REDACTED]" in record.msg
