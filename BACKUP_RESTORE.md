# Database backup and restore runbook

This runbook describes the local Compose workflow. Production backups must use the hosting provider's encrypted backup service, private storage, retention controls, and point-in-time recovery where available. Never store a dump in source control or beside an exposed web root.

Application retention is operator-controlled: contacts and campaign events remain until manually deleted. There is no automatic purge. Contact deletion preserves a matching global suppression entry so re-import cannot restore eligibility.

## Create a local backup

From the project root in PowerShell, with PostgreSQL running:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$container = docker compose ps -q postgres
docker compose exec postgres sh -c "pg_dump -U bms -Fc bms -f /tmp/bms-$stamp.dump"
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed' }
docker cp "${container}:/tmp/bms-$stamp.dump" "backups/bms-$stamp.dump"
if ($LASTEXITCODE -ne 0) { throw 'Copying database backup failed' }
```

Store backups on encrypted storage with access limited to the operator. Define and approve retention before storing real contact data. The local development database contains synthetic data only.

## Restore drill to a separate database

Do not restore over the active database for a drill. Create a disposable database in the local container and restore there:

```powershell
docker compose exec postgres createdb -U postgres bms_restore_check
$container = docker compose ps -q postgres
docker cp "backups\bms-YYYYMMDD-HHMMSS.dump" "${container}:/tmp/bms-restore-check.dump"
docker compose exec postgres pg_restore -U postgres -d bms_restore_check --no-owner /tmp/bms-restore-check.dump
docker compose exec postgres psql -U postgres -d bms_restore_check -c "SELECT version_num FROM alembic_version"
```

Then start a temporary API configured with the restored database and confirm `/ready`, login, and read-only report endpoints. Record the backup timestamp, restore duration, migration revision, and outcome in the operations log. Remove the disposable restore database only after recording the result:

```powershell
docker compose exec postgres dropdb -U postgres bms_restore_check
```

## Automated local backup and restore drill (Python)

Run the automated backup and verification drill:

```powershell
backend\.venv\Scripts\python backend\scripts\backup_drill.py
```

This creates an atomic snapshot using the SQLite online backup API, tests PRAGMA integrity, validates table row counts and latest Alembic migration version, and outputs a JSON verification report. To verify an existing backup file without taking a new snapshot:

```powershell
backend\.venv\Scripts\python backend\scripts\backup_drill.py --verify-only backups/bms-sqlite-YYYYMMDD-HHMMSS.db
```

These commands have not been run in this development session. A production restore drill and retention approval remain release gates.
