# Security Policy and Vulnerability Management

This document outlines the security architecture, vulnerability response process, and operational hardening guidelines for the Bulk Mail Management System (BMS).

---

## 1. Security Architecture Principles

- **Single-Operator Boundary**: BMS is designed strictly as a single-tenant internal management system. Public registration is disabled.
- **Fail-Closed Access Control**: Unauthenticated requests to administrative endpoints are rejected with `401 Unauthorized` before any business logic executes.
- **Two-Factor Authentication (RFC 6238 TOTP)**: Operator access supports Time-Based One-Time Passwords with dynamic truncation and clock-drift protection.
- **Log Interception & PII Redaction**: All log records pass through `PIIRedactingFilter` to prevent leakage of email addresses, session cookies, bearer tokens, passwords, and message payloads.
- **Cryptographic Signatures**:
  - Inbound provider webhooks require HMAC-SHA256 signature verification (`X-Provider-Signature`) and replay defense via UUID-based `event_id` tracking.
  - Public unsubscribe links are signed with a stable key (`UNSUBSCRIBE_SIGNING_KEY`) using HMAC-SHA256, protecting against parameter tampering without leaking recipient emails in URLs.
- **Formula Injection Defense**: All exported cells starting with `=`, `+`, `-`, or `@` are neutralized with single-quote escaping (`safe_csv_cell`).
- **Template Sanitization**: User-supplied email bodies are sanitized using `nh3` against a strict whitelist of safe HTML tags and attributes; `<script>`, `<iframe>`, and event handlers (`onload=`, `onclick=`) are stripped.

---

## 2. Vulnerability Reporting Process

If you discover a potential security vulnerability within this repository or deployment:
1. **Confidential Communication**: Contact the designated system operator directly via secure internal channels. Do NOT open public GitHub issues.
2. **Report Contents**:
   - Detailed description of the vulnerability and attack vector.
   - Step-by-step reproduction steps or minimal proof-of-concept.
   - Estimated impact and affected components.
3. **SLA and Response Window**:
   - **Initial Triage**: Within 24 hours of receipt.
   - **Remediation Plan**: Within 72 hours for critical/high vulnerabilities.
   - **Fix Deployment**: Deployed through isolated staging rehearsal before production release.

---

## 3. Dependency Review & Continuous Auditing

BMS uses pinned lockfiles (`uv.lock` for backend Python packages; `package-lock.json` for frontend npm dependencies).

### Running Security Audits:
```bash
# Python backend dependency review
python backend/scripts/security_audit.py

# Frontend npm audit
npm --prefix frontend audit
```

### Response Process for Upstream Vulnerabilities:
1. Assess exposure against the local single-tenant threat model.
2. If affected, update package version in `backend/pyproject.toml` or `frontend/package.json`.
3. Regenerate lockfile and run the full automated test suite:
   ```bash
   pytest backend/tests
   npm --prefix frontend run build
   ```
4. Perform the staging rehearsal drill before production promotion:
   ```bash
   python backend/scripts/staging_e2e_drill.py
   ```
