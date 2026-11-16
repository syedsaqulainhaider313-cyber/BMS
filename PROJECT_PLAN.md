# Project plan and requirements

## Product direction

Build a private operations console used by one developer/operator to manage legitimate, permission-based email campaigns for their work. It is not a tenant-facing SaaS and does not need self-service signup or client accounts. Client/workspace labels may be used to keep records and reports organized, without giving clients logins.

Initial platform: responsive web application accessed from Windows, with a local mock email provider. Production sending and paid services are explicitly deferred. Future sending may use Amazon SES API or SMTP through a provider adapter, after the operator approves the account, spend, region, and configuration.

## Requirements

### Functional

1. Single operator authentication, with secure session handling and no public registration.
2. Campaign/client labels for separating contacts, imports, templates, campaigns, and reports.
3. CSV import preview, column mapping, validation, duplicate detection, and auditable import results. Reject unsupported formats safely; never silently opt a person in.
4. Contact record with email, optional profile fields, status, consent evidence (source, timestamp, purpose/list), and provenance.
5. Suppression ledger covering unsubscribe, hard bounce, complaint, and manual suppression; suppression is global by normalized address and cannot be overridden by re-import.
6. Template editing with subject, HTML/text bodies, sender identity, address/physical mailing details as applicable, and mandatory unsubscribe link placeholder.
7. Campaign draft, audience preview, explicit compliance checklist, test-to-operator preview, schedule/pause/cancel controls, and immutable recipient snapshot at launch.
8. Queue with provider quota/rate limits, idempotency, retry policy for transient failures only, and circuit breaker/pause controls.
9. Event processing for accepted/delivered (where available), bounce, complaint, and unsubscribe; idempotent webhook handling and audit history.
10. Campaign reporting and CSV export for operational review, with careful distinction between accepted and delivered.
11. Provider settings visible to the operator; secrets server-side only and masked. Local mock is the only enabled adapter in the initial scaffold.

### Non-functional and safety

- One operator, least privilege, no public tenant creation.
- Audit campaign approvals, imports, suppression changes, provider changes, and lifecycle actions.
- Protect contact data at rest and in transit in production; define retention/deletion policy before launch.
- Backups and restore drills before production data is stored.
- No purchased, scraped, or otherwise unauthorized lists. No spam-filter bypass or inbox-time guarantees.
- Enforce suppression immediately before send; unsubscribe should take effect without waiting for a batch refresh.
- Respect provider quota, account sending status, and local rate caps. Fail closed when event/suppression checks are unavailable.

## Development phases and exit checks

### 0. Foundation (current)

Repository docs, environment template, API health endpoint, UI shell, local PostgreSQL/Redis definitions, mock-only setting. Exit: both app shells start locally; no real send route or credentials are enabled.

### 1. Data model and operator access

SQLAlchemy models and Alembic migrations for operator, client labels, contacts, consent evidence, suppression, templates, campaigns, recipient snapshots, events, and audit log. Add authentication with secure cookie/session, CSRF protection, and no signup. Exit: migrations and authorization boundaries reviewed.

### 2. Contact intake and consent

CSV preview/mapping, validation, deduplication, consent provenance, suppression-aware import, deletion/export. Exit: opt-in cannot be manufactured by import; tests cover re-import after unsubscribe and duplicate normalization.

### 3. Templates and campaign review

Safe template variables, HTML/text preview, footer requirements, test message to operator only, audience count with exclusions, launch checklist and confirmation. Exit: campaign snapshot is auditable and only eligible contacts qualify.

### 4. Queue and mock sending

Redis/Celery worker, idempotent outbox, bounded retries, rate limits, pause/cancel, deterministic mock outcomes, event ingestion. Exit: integration tests show unsubscribe/bounce/complaint suppression blocks pending recipients and worker restarts do not duplicate sends.

### 5. Reporting and operational controls

Campaign metrics, event timeline, export, health/readiness, backup/restore, log redaction, retention policy, alerting. Exit: restore and incident procedures are documented and rehearsed.

### 6. Production hardening

TLS, secrets manager, operator MFA, security review, dependency updates, monitored deployment, database backups, webhook signature verification, abuse caps, and a test environment. Exit: security/compliance checklist approved by operator.

### 7. Paid provider decision (later, explicit approval)

Compare current provider terms/costs and requirements. If SES is selected, verify domain/sender, DNS authentication, region, production access, quotas, bounce/complaint event path, credentials, spend limits, and rollback. No paid service is to be purchased or configured as part of the present scaffold.

## Initial acceptance criteria

- `docker compose config` parses and local Postgres/Redis services are defined.
- API returns a health response and documents itself locally.
- UI renders a clear “mock mode / no real sending” notice.
- `.env.example` contains placeholders only; `.env` and contact data are ignored by Git.
- Documentation covers requirements, architecture, setup, workflows, API, deployment, and future SES.
- No campaign send implementation points at an external provider.
