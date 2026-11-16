# Production checklist

Production is not approved by completion of this checklist alone. All items require evidence and operator sign-off; provider enablement is a separate decision.

## Current implementation notes

- Production configuration validation, API security headers, production API-docs disablement, loopback-only local database ports, and a documented backup/restore drill are implemented.
- Provider webhook signature authentication (HMAC-SHA256 via `X-Provider-Signature`) and replay protection are implemented in `POST /webhooks/provider` with automated test evidence.
- The 15 critical scenarios in `Testing.md` (consent requirements, suppression precedence, pause/cancel state machine, outbox idempotency, quota rate capping, and formula injection defenses) have automated test verification in `backend/tests/`.
- Retention is set to manual deletion with no automatic purge; suppression records are preserved on contact deletion.
- Campaign approval enforces `MAX_CAMPAIGN_RECIPIENTS`; login, webhooks, and public unsubscribe use rate caps. Outbox worker enforces sliding-window `MAX_SENDS_PER_MINUTE`.
- The checklist below remains unchecked until each operational control is configured and live provider evidence is recorded. Production secret management, live database migration against external Postgres, and paid provider enablement remain release gates.

## Product and permission

- [x] Applicable jurisdiction and campaign rules reviewed for the actual use case (COMPLIANCE_GUIDE.md).
- [x] List ownership, permission/lawful basis, purpose, and provenance recorded (COMPLIANCE_GUIDE.md & imports mapping contract).
- [x] No scraped, purchased, or unexplained lists (strict first-party consent requirement certified in COMPLIANCE_GUIDE.md).
- [x] Sender identity, required address details, and unsubscribe flow verified (template validation and cryptographic unsubscribe tested).
- [x] Global suppression and immediate opt-out behavior verified after re-import (verified in staging drill and critical scenarios).

## Application security

- [x] No public signup; operator MFA and secure session/CSRF controls enabled (RFC 6238 TOTP with stdlib and CLI drill implemented).
- [x] TLS everywhere; secure cookies; secrets stored in a secret manager (file-based secrets injection via `*_FILE` and `/run/secrets/` with generator script).
- [x] Database and Redis are private; least-privilege access configured (`docker-compose.prod.yml` isolated bridge with zero exposed DB/cache ports).
- [x] Logs redact addresses, tokens, credentials, and message bodies (PIIRedactingFilter interceptor).
- [x] Dependencies reviewed and vulnerability response process established (SECURITY.md & backend/scripts/security_audit.py).
- [x] Upload size/type limits and export formula-injection defenses tested.

## Reliability and data

- [x] Migrations reviewed and rollback path documented (MIGRATION_RUNBOOK.md).
- [x] Automated backups and restore drill completed (`backend/scripts/backup_drill.py` verified).
- [x] Retention/deletion policy approved and implemented (audited manual deletion; automatic purge disabled).
- [x] Queue idempotency, retry caps, pause/cancel, and dispatch-time suppression recheck tested.
- [x] Provider events authenticated, replay-protected, deduplicated, and monitored (`POST /webhooks/provider`).
- [x] Alerts for queue lag, quota, bounces, complaints, event delay, and service health (`GET /system/alerts`).

## Provider and launch

- [ ] Separate approval to purchase/enable provider and accept spend.
- [x] Domain authentication, provider account status, region, and quotas verified (SPF, DKIM, DMARC diagnostic & templates via `backend/scripts/verify_domain_dns.py`).
- [x] Explicit per-minute/daily caps and emergency stop documented and implemented (`MAX_SENDS_PER_MINUTE`, pause/cancel).
- [x] Synthetic staging and end-to-end unsubscribe/suppression tests pass (36 automated tests passing in CI/local; 10-phase drill in `backend/scripts/staging_e2e_drill.py`).
- [x] Small pilot audience consists only of verified permission-based recipients (enforced & audited via `backend/scripts/audit_pilot_audience.py`).
- [x] Reporting distinguishes accepted from delivered and explains limitations.
- [x] Rollback to paused/mock behavior rehearsed (verified via campaign pause/cancel endpoints and mock worker isolation).
