# Micro tasks

Small implementation units. “Ready” means the product decision is settled and the task can be picked up without inventing policy.

## Foundation

- [x] Add pinned dependency lockfile for Python 3.12 (`backend/uv.lock`).
- [x] Add npm lockfile for frontend dependencies.
- [x] Add shared formatting/lint configuration and editor guidance.
- [x] Add API configuration validation and separate liveness/readiness checks.
- [x] Add frontend environment typing and API client shell.
- [x] Document local data reset procedure. Compose profile split remains optional for this two-service local stack.
- [x] Validate production runtime secrets and safety settings at startup.
- [x] Add baseline API response security headers and disable interactive docs in production mode.

## Data and access

- [x] Define SQLAlchemy base and migration environment.
- [x] Model contact, consent evidence, suppression, and import batch.
- [x] Normalize email addresses and add uniqueness/index strategy.
- [x] Add single-operator login without public signup.
- [x] Add secure cookie, CSRF, session expiry, and audit logging.
- [x] Add manual contact deletion with historical redaction and audited global suppression controls.
- [x] Add signed, idempotent unsubscribe handling with a stable signing key.
- [x] Add audited manual event timeline deletion under the selected retention policy.

## Consent and intake

- [x] Build CSV header/row preview endpoint with file-size limits.
- [x] Implement explicit field mapping and validation report.
- [x] Reject malformed/oversized imports and neutralize spreadsheet formulas in campaign exports.
- [x] Require consent source, timestamp, purpose, and provenance on every imported row.
- [x] Ensure imports cannot remove suppression or manufacture opt-in.

## Campaign lifecycle

- [x] Add template HTML sanitization and text alternative.
- [x] Require sender identity, mailing address, and unsubscribe placeholder.
- [x] Implement eligibility preview with exclusion reasons.
- [x] Freeze campaign content and recipient snapshot at approval.
- [x] Add approve, pause, resume, and cancel state transitions (scheduling deferred).
- [x] Expose pause/resume/cancel in the operator UI; paused mock jobs wait for resume and cancelled pending jobs are terminal.

## Queue and events

- [x] Add mock provider protocol and deterministic outcomes.
- [x] Add outbox/idempotency key and bounded transient retries.
- [x] Recheck consent/suppression immediately before mock dispatch.
- [x] Pause on quota, provider failures, or event pipeline interruption (worker sliding-window MAX_SENDS_PER_MINUTE rate cap implemented and verified).
- [x] Deduplicate mock events and suppress mock hard bounces/complaints; signed provider webhook ingestion implemented with HMAC-SHA256 and replay protection.
- [x] Implement operator Multi-Factor Authentication (RFC 6238 TOTP using stdlib `hmac`/`hashlib`, setup CLI script `setup_mfa.py`, and frontend 2FA prompt).
- [x] Implement PII and sensitive token log redaction filter (`logging_config.PIIRedactingFilter`) protecting emails, tokens, cookies, passwords, and message bodies.
- [x] Document database migration lineage and deterministic rollback runbook (`MIGRATION_RUNBOOK.md`).
- [x] Add file-based secrets resolution (`*_FILE` and `/run/secrets/`) and production cryptographic secrets generator (`backend/scripts/generate_prod_secrets.py`).
- [x] Provide production multi-stage `Dockerfile` (Node 20 builder + Python 3.12 non-root system user) and `docker-compose.prod.yml` with zero-exposure private network topology.
- [x] Abstract email delivery provider protocol (`backend/app/provider.py`) with deterministic `MockEmailProvider`.
- [x] Create automated 10-phase staging end-to-end rehearsal drill (`backend/scripts/staging_e2e_drill.py`).
- [x] Establish vulnerability response process (`SECURITY.md`) and pre-deployment automated configuration security audit tool (`backend/scripts/security_audit.py`).
- [x] Establish legal compliance standards, list ownership contracts, and consent verification rules (`COMPLIANCE_GUIDE.md`).
- [x] Enable directory-independent Alembic execution with offline PostgreSQL transactional DDL verification (`alembic upgrade head --sql`).
- [x] Implement domain DNS authentication validator and record generator for SPF, DKIM, and DMARC (`backend/scripts/verify_domain_dns.py`).
- [x] Implement pilot audience consent validator and permission auditor (`backend/scripts/audit_pilot_audience.py`).
- [x] Establish comprehensive GitHub Actions CI/CD automation pipeline (`.github/workflows/ci.yml`) enforcing linting, full test suite, security audit, backup drill, staging drill, PostgreSQL DDL generation, and container image build.

## Reports and readiness

- [x] Separate accepted from delivered metrics and document provider delivery limitations in UI and API responses.
- [x] Add audit-friendly campaign event timeline and formula-safe export.
- [x] Document backup/restore and record operator-selected manual retention policy; automatic purge is intentionally disabled.
- [x] Implement automated local database backup and isolated restore drill script (`backend/scripts/backup_drill.py`); verified snapshot integrity and Alembic revision checks.
- [x] Add and run automated `Testing.md` critical-path coverage (36 passing automated tests covering all 15 critical scenarios, unit checks, webhooks, audit trails, alerts, MFA, logging, secret loading, staging rehearsal, security audit, DNS, and pilot audience in 3.5s).
- [ ] Complete `Production Checklist.md` before considering live service (single remaining release gate: operator purchase/enablement of external provider).
