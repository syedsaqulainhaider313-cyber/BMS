# Local setup guide

## Prerequisites

- Windows 10/11
- Git
- Docker Desktop with Docker Compose
- Node.js LTS (npm included)
- Python 3.12 or newer

No AWS account, domain, SMTP credentials, or paid service is needed for local scaffold work.

## First-time setup

From the repository root in PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up -d postgres redis
```

API terminal:

```powershell
Set-Location backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

UI terminal:

```powershell
Set-Location frontend
npm install
npm run dev
```

Visit the URL printed by Vite, normally `http://localhost:5173`. API health: `http://localhost:8000/health`; interactive API reference: `http://localhost:8000/docs`.

## Environment

`.env.example` documents local defaults. Copy it to `.env`, then replace development-only secrets if needed. Never commit `.env`, AWS keys, SMTP passwords, exports, or real contact data. Keep `EMAIL_PROVIDER=mock` for this phase. `DATABASE_URL` and `REDIS_URL` point to local Compose services.

## Common operations

- Start dependencies: `docker compose up -d postgres redis`
- Stop dependencies: `docker compose down`
- View service status: `docker compose ps`
- API docs: `http://localhost:8000/docs`
- Frontend dev server: `npm run dev` from `frontend`
- API dependencies: `python -m pip install -e ".[dev]"` from `backend`
- Reset local synthetic data: follow [`LOCAL_DATA_RESET.md`](LOCAL_DATA_RESET.md)
- Backup/restore drill: follow [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md)

The API reads and writes local campaign data after migrations are applied. Real sending remains unavailable.
