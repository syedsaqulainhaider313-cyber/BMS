from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import HTTPException, Request, Response
from pwdlib import PasswordHash
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AuditEvent, OperatorSession
from app.settings import settings

COOKIE_NAME = "bms_session"
password_hash = PasswordHash.recommended()


def _normalize_b32(secret: str) -> bytes:
    clean = secret.strip().replace(" ", "").upper()
    missing_padding = len(clean) % 8
    if missing_padding:
        clean += "=" * (8 - missing_padding)
    return base64.b32decode(clean, casefold=True)


def generate_totp_secret() -> str:
    """Generate a high-entropy 160-bit Base32 secret for RFC 6238 TOTP."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def compute_totp_code(secret_b32: str, for_time: int | None = None) -> str:
    """Compute a 6-digit TOTP code using HMAC-SHA1 as per RFC 6238."""
    if for_time is None:
        for_time = int(time.time())
    counter = for_time // 30
    key = _normalize_b32(secret_b32)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1000000:06d}"


def verify_totp_code(secret_b32: str, code: str, window: int = 1) -> bool:
    """Verify a 6-digit TOTP code allowing ±window steps (30 seconds each) clock drift."""
    cleaned = code.strip().replace(" ", "")
    if len(cleaned) != 6 or not cleaned.isdigit():
        return False
    now = int(time.time())
    for step in range(-window, window + 1):
        target_time = now + (step * 30)
        expected = compute_totp_code(secret_b32, target_time)
        if hmac.compare_digest(expected, cleaned):
            return True
    return False


def get_totp_uri(secret_b32: str, account_name: str, issuer: str = "BMS") -> str:
    """Build standard otpauth:// URI for authenticator applications."""
    clean_secret = secret_b32.strip().replace(" ", "").upper()
    label = quote(f"{issuer}:{account_name}")
    issuer_param = quote(issuer)
    return f"otpauth://totp/{label}?secret={clean_secret}&issuer={issuer_param}&algorithm=SHA1&digits=6&period=30"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def audit(db: Session, action: str, *, detail: str | None = None) -> None:
    db.add(AuditEvent(actor=settings.operator_username, action=action, detail=detail))


def _session_id(raw: str) -> str:
    return hmac.new(settings.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def create_session(db: Session, response: Response) -> str:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.execute(delete(OperatorSession).where(OperatorSession.expires_at <= now))
    db.add(
        OperatorSession(
            id=_session_id(raw),
            csrf_token=csrf,
            expires_at=now + timedelta(seconds=settings.session_max_age_seconds),
        )
    )
    response.set_cookie(
        COOKIE_NAME,
        raw,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    return csrf


def authenticate_request(request: Request, db: Session) -> OperatorSession:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail="Authentication required")
    row = db.get(OperatorSession, _session_id(raw))
    expires_at = row.expires_at if row is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if row is None or expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Session expired")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, row.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return row
