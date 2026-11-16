# Project tasks and milestones

## Current milestone: Runtime verification and production readiness

- [x] Confirm workspace has no existing app repository.
- [x] Confirm project direction from the referenced conversation.
- [x] Identified and applied "ponytail" skill (lazy senior developer: ladder - YAGNI, reuse existing code, stdlib first, native platform features, root-cause fixes, never sacrifice validation/security/accessibility).
- [x] Create React/FastAPI mock-only scaffold and core project documentation.
- [x] Initialize a local Git repository and add static/build CI after conventions were settled.
- [x] Generate npm lockfile, install locked frontend dependencies, and complete a production frontend build.
- [x] Compile backend/migration Python syntax with Python 3.11.
- [x] Apply the complete migration chain to a disposable SQLite database and confirm Alembic reaches head; PostgreSQL verification remains pending.
- [x] Generate the Python dependency lockfile for Python 3.12.
- [x] Run backend Ruff checks and Python syntax compilation.
- [x] Complete backend app import and `/health` handler smoke check (HTTP TestClient hangs in this environment; PostgreSQL runtime verification remains pending).
- [x] Refresh the Python 3.12 dependency lock and install the declared backend dependencies.
- [ ] Verify the locked backend runtime against PostgreSQL; local migration verification used SQLite.

## Implemented data and operator access

- [x] Add SQLAlchemy, Alembic, and initial schema for contacts, consent, suppressions, and import batches.
- [x] Implement single-operator authentication and audit events.
- [x] Add database readiness check and migrations workflow.
- [x] Add manual contact deletion and campaign event deletion under the selected manual retention policy.
- [x] Add manual/global suppression management and signed unsubscribe handling.
- [x] Add campaign pause/resume/cancel controls to the operator UI and preserve pending mock jobs across pauses.
- [x] Replace fixed-zero overview cards with authenticated database-backed campaign, consent, acceptance, and suppression totals.

## Later milestones

- [x] Contact CSV import preview and safe commit (SQLite migration verified; full API runtime check pending).
- [x] Template editor and unsubscribe-ready preview (API runtime check pending).
- [x] Campaign review, recipient snapshots, approval, pause/cancel (API runtime check pending).
- [x] Mock queue worker and idempotent event handling (API runtime check pending).
- [x] Reports and exports (API runtime check pending).
- [x] Complete automated critical-path tests (23 passing tests covering all 15 scenarios in Testing.md).
- [x] Implement signed provider webhooks (`POST /webhooks/provider`) with HMAC-SHA256, replay protection, and bounce/complaint/unsubscribe ingestion.
- [x] Implement sliding-window rate capping (`MAX_SENDS_PER_MINUTE`) in the outbox worker without dropping pending recipients.
- [x] Upgrade frontend UI/UX to senior-level standards: tabbed view navigation, accessible confirmation dialogs, search/filter, loading states, and live system monitoring.
- [x] Add operational health alert endpoint (`GET /system/alerts`) monitoring queue lag and bounce/complaint thresholds.
- [x] Add administrative audit log inspection endpoint (`GET /audit-logs`) and frontend audit trail viewer.
- [x] Implement automated backup and restore drill tool (`backend/scripts/backup_drill.py`) with verified atomic snapshot and restore drill report.
- [ ] Separate decision and approval for SES or another paid provider.
