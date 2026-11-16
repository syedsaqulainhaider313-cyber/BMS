# Database Migration and Rollback Runbook

This document details the database schema migration history, validation procedures, and deterministic rollback paths for the Bulk Mail Management System (BMS).

---

## 1. Schema Revision Lineage

All migrations are located in [`backend/migrations/versions/`](file:///c:/Users/Asus/Desktop/BMS-Project/backend/migrations/versions/).

| Revision | Down Revision | Description | Supported Engines |
|---|---|---|---|
| `0001_initial_data_foundation` | `None` (base) | Core schema: `contacts`, `consent_evidence`, `suppressions`, `audit_events`. Sets up normalization, indexes, and primary keys. | SQLite, PostgreSQL |
| `0002_operator_access` | `0001_initial_data_foundation` | Operator session table `operator_sessions` with hashed session IDs, CSRF tokens, and expiry timestamps. | SQLite, PostgreSQL |
| `0003_import_review` | `0002_operator_access` | `import_batches` table tracking staged CSV uploads, column mappings, and verification tokens. | SQLite, PostgreSQL |
| `0004_email_templates` | `0003_import_review` | `email_templates` table storing sanitised HTML, text alternatives, sender metadata, and physical address. | SQLite, PostgreSQL |
| `0005_campaign_review` | `0004_email_templates` | `campaigns` table with frozen recipient snapshots, immutable content hashes, and state machine columns. | SQLite, PostgreSQL |
| `0006_mock_outbox` | `0005_campaign_review` | `outbox_messages` (idempotency keys, retry tracking, backoff) and `campaign_events` (audit timeline). | SQLite, PostgreSQL |
| `0007_manual_retention` | `0006_mock_outbox` | Aligns `outbox_messages.contact_id` to nullable with `ON DELETE SET NULL`, enabling GDPR/compliance contact deletion while preserving campaign audit delivery counts. | SQLite, PostgreSQL |

---

## 2. Forward Migration (Upgrade)

### In Development / Local Test (SQLite)
Tables and Alembic versions are automatically created and initialized by `Base.metadata.create_all()` and Alembic:
```bash
# Verify current Alembic revision in database
python -c "from app.db import SessionLocal; from sqlalchemy import text; s=SessionLocal(); print(s.execute(text('SELECT version_num FROM alembic_version')).scalar()); s.close()"
```

### In Production / Staging (PostgreSQL)
Ensure `DATABASE_URL` is configured in environment, then execute:
```bash
# Check current database revision
alembic current

# Check pending migrations without applying
alembic show head

# Apply all pending migrations to head
alembic upgrade head

# Verify head revision is 0007_manual_retention
alembic current
```

---

## 3. Rollback Runbook (Downgrade)

Every migration file provides a dialect-aware `downgrade()` function supporting both PostgreSQL and SQLite (using batch mode).

### Rolling back a single migration step
To undo the most recent migration:
```bash
alembic downgrade -1
```

### Rolling back to a specific target revision
```bash
# Example: Rollback from 0007 to 0006
alembic downgrade 0006_mock_outbox

# Example: Rollback to initial foundation
alembic downgrade 0001_initial_data_foundation
```

### Step-by-Step Rollback Drill & Safety Rules

1. **Pre-Rollback Snapshot**:
   Always run an atomic database backup before executing downgrades:
   ```bash
   python backend/scripts/backup_drill.py
   ```
2. **Stop Ingestion & Sending Workers**:
   Ensure no background workers or import tasks are in flight:
   - Worker processes running `backend/app/worker.py` must be terminated or paused.
3. **Run Downgrade**:
   ```bash
   alembic downgrade <target_revision>
   ```
4. **Post-Rollback Verification**:
   Verify database connectivity and schema integrity:
   ```bash
   python -c "from app.db import SessionLocal; from sqlalchemy import text; s=SessionLocal(); print('Version:', s.execute(text('SELECT version_num FROM alembic_version')).scalar()); s.close()"
   ```
5. **Resume Workers**:
   Restart worker process once schema downgrade is confirmed.
