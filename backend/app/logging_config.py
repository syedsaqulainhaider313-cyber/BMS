"""Application logging configuration with PII and sensitive token redaction.

Ensures that log outputs never leak recipient email addresses, authorization
credentials, session cookies, CSRF tokens, cryptographic keys, or message bodies.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Pre-compiled regex patterns for sensitive data
EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
COOKIE_REGEX = re.compile(r"bms_session=[A-Za-z0-9_\-]+", re.IGNORECASE)
BEARER_REGEX = re.compile(r"Authorization:\s*(?:Bearer\s+)?[A-Za-z0-9_\-\.]+", re.IGNORECASE)
SIGNATURE_REGEX = re.compile(
    r"(X-(?:Provider-)?Signature(?:-SHA256)?[:=]\s*)[a-fA-F0-9]+", re.IGNORECASE
)
KEY_VALUE_SECRET_REGEX = re.compile(
    r'(?i)(password|secret|token|csrf_token|signing_key|totp_secret)\s*[:=]\s*["\']?([^"\'\s&,;]+)["\']?'
)
JSON_SECRET_REGEX = re.compile(
    r'(?i)"(password|secret|token|csrf_token|signing_key|totp_secret)"\s*:\s*"[^"]+"'
)
BODY_HTML_REGEX = re.compile(r"<html[\s\S]*?</html>", re.IGNORECASE)


def redact_text(text: str) -> str:
    """Apply all redaction rules to a given string."""
    if not text:
        return text
    # Redact HTML bodies first if present
    text = BODY_HTML_REGEX.sub("[REDACTED_HTML_BODY]", text)
    # Redact credentials and tokens
    text = JSON_SECRET_REGEX.sub(r'"\1": "[REDACTED]"', text)
    text = KEY_VALUE_SECRET_REGEX.sub(r"\1=[REDACTED]", text)
    text = COOKIE_REGEX.sub("bms_session=[REDACTED]", text)
    text = BEARER_REGEX.sub("Authorization: [REDACTED]", text)
    text = SIGNATURE_REGEX.sub(r"\1[REDACTED]", text)
    # Redact email addresses
    text = EMAIL_REGEX.sub("[REDACTED_EMAIL]", text)
    return text


def _redact_object(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        redacted = [_redact_object(v) for v in obj]
        return tuple(redacted) if isinstance(obj, tuple) else redacted
    return obj


class PIIRedactingFilter(logging.Filter):
    """Logging filter that redacts emails, tokens, and credentials in LogRecords."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = _redact_object(record.args)
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Attach the PII redaction filter to the root and common framework loggers."""
    redacting_filter = PIIRedactingFilter()

    # Configure root logger
    root_logger = logging.getLogger()
    if not any(isinstance(f, PIIRedactingFilter) for f in root_logger.filters):
        root_logger.addFilter(redacting_filter)

    # Attach to existing handlers
    for handler in root_logger.handlers:
        if not any(isinstance(f, PIIRedactingFilter) for f in handler.filters):
            handler.addFilter(redacting_filter)

    # Attach specifically to uvicorn and sqlalchemy loggers
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine", "alembic"):
        named_logger = logging.getLogger(logger_name)
        if not any(isinstance(f, PIIRedactingFilter) for f in named_logger.filters):
            named_logger.addFilter(redacting_filter)
        for handler in named_logger.handlers:
            if not any(isinstance(f, PIIRedactingFilter) for f in handler.filters):
                handler.addFilter(redacting_filter)
