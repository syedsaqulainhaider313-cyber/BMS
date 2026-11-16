# Project memory

Durable context and decisions only. Update this file when a product direction or operational assumption changes.

## Current state

- Local mock-only BMS application with a React/Vite UI and FastAPI/PostgreSQL backend.
- FastAPI liveness and database-readiness routes, React dashboard shell, SQLAlchemy models, and Alembic migrations exist.
- Initial schema covers normalized contacts, consent evidence, global suppression entries, staged import batches, operator sessions, audit events, templates, campaigns, outbox jobs, and campaign events. Consent evidence is recorded through CSV import; suppression can be managed manually or by unsubscribe.
- Operator login uses an environment-configured password hash and opaque server-side sessions; the browser dashboard is gated by login. Set `SECURE_COOKIES=true` behind HTTPS.
- Campaign drafts reference saved templates and exact consent purposes. Approval freezes content and recipients. A database outbox supports idempotent mock jobs; the worker rechecks consent/suppression and records deduplicated deterministic events. No external sending path exists.
- CSV import preview/commit requires explicit consent mapping, stages rows for 30 minutes, and rechecks suppression at commit.
- Email templates are persisted after HTML sanitization, require plain text and sender/mailing identity, and have one unsubscribe placeholder in each body. Browser preview uses a sandboxed iframe.
- UI communicates mock-only mode; campaign review and a local database outbox worker are implemented, but there is no real sending workflow.
- The mock worker uses idempotent outbox keys, bounded retries, dispatch-time consent/suppression checks, deterministic outcomes, and deduplicated campaign events.
- Campaign reports separate accepted from delivered, show pending/failed/suppressed counts, and provide audited formula-safe CSV exports.
- Production configuration validation, baseline API security headers, docs disablement in production mode, and loopback-only Compose ports are in place. Backup/restore steps are documented, but no backup or restore drill has been run.
- Retention decision: keep contact data and campaign events until an operator manually deletes them. There is no automatic purge. Contact deletion redacts snapshots/outbox rows and invalidates pending previews; existing global suppression entries remain to prevent reimport from restoring eligibility.
- A stable `UNSUBSCRIBE_SIGNING_KEY` signs per-campaign/contact links without embedding email addresses. GET displays confirmation; POST applies global suppression idempotently.
- PostgreSQL and Redis are defined for local Compose use.
- Paid services and SES are not configured.
- Applied 'ponytail' skill (lazy senior developer: ladder - YAGNI, reuse existing code, stdlib first, native platform features, root-cause fixes, never sacrifice validation/security/accessibility).
- Automated test suite covers all 15 critical scenarios in Testing.md plus webhooks, alerts, unit checks, MFA, logging, secret file loading, staging rehearsal, security audit, domain DNS, and pilot audience validation with 36 passing tests in `backend/tests/`.
- Full automated CI/CD pipeline configured in `.github/workflows/ci.yml` running frontend build, backend lint/test, security audits, drills, and Docker image build.
- Domain authentication diagnostic & record generator implemented in `backend/scripts/verify_domain_dns.py` for SPF, DKIM, and DMARC compliance.
- Pilot audience consent verification and risk audit implemented in `backend/scripts/audit_pilot_audience.py`.
- Offline PostgreSQL migration DDL verified end-to-end via `alembic upgrade head --sql` using relative `%(here)s/migrations` pathing.
- Security and vulnerability disclosure runbook established in `SECURITY.md` alongside pre-deployment audit tool `backend/scripts/security_audit.py`.
- Legal compliance, list ownership contracts, and consent standards established in `COMPLIANCE_GUIDE.md`.
- Email delivery provider decoupled via `backend/app/provider.py` abstract protocol with deterministic `MockEmailProvider`.
- Automated 10-phase synthetic staging end-to-end drill implemented in `backend/scripts/staging_e2e_drill.py` verifying full lifecycle integrity.
- Production containerization configured with multi-stage `Dockerfile` (Node 20 builder + Python 3.12 unprivileged runner) and `docker-compose.prod.yml` with private bridge network.
- Container secrets injection implemented via `*_FILE` settings and `/run/secrets/` with cryptographic generator `backend/scripts/generate_prod_secrets.py`.
- Inbound provider webhooks (`POST /webhooks/provider`) verify HMAC-SHA256 signatures, deduplicate events via `event_id`, and ingest hard bounces, complaints, and unsubscribes into global suppression.
- Worker rate-limiting enforces `MAX_SENDS_PER_MINUTE` over a 60-second sliding window without dropping queued recipients.
- Operator Multi-Factor Authentication (RFC 6238 TOTP) implemented using Python standard library (`hmac`, `hashlib`, `struct`, `base64`), with setup CLI tool `backend/scripts/setup_mfa.py` and frontend 2FA prompt.
- PII and sensitive token redaction filter (`logging_config.PIIRedactingFilter`) automatically redacts email addresses, session cookies, bearer tokens, passwords, and message bodies across all application and server logs.
- Database migration lineage and rollback runbook (`MIGRATION_RUNBOOK.md`) documented and verified for both PostgreSQL and SQLite.
- Frontend UI elevated to senior-level standards with tabbed section navigation, accessible confirmation modals, live preview, search/filter, system health monitoring, and 2FA prompt.

## Persistent constraints

- Sole developer/operator; internal tool, not multi-tenant SaaS.
- Permission-based campaigns only.
- Require consent evidence; maintain global unsubscribe/suppression; process bounces and complaints.
- Enforce quota and rate caps, audit actions, and fail closed.
- No spam-filter bypass or inbox placement guarantees.
- Do not purchase/configure external services without later approval.

## Naming note

The repository already had uppercase `ARCHITECTURE.md`, `README.md`, `DEPLOYMENT.md`, and `PROJECT_PLAN.md`. Keep them as canonical files and use this doc set to link to them; avoid case-only duplicate files on Windows.
