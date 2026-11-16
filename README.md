# Bulk Mail Management System (BMS)

Internal campaign operations tool for one developer/operator. It is designed only for permission-based campaigns. The initial provider is a local mock: sending to real recipients is deliberately unavailable until a provider and compliance controls are reviewed and explicitly configured.

## Project status

The local mock-only application includes operator login, consent-aware CSV intake, global suppression and unsubscribe handling, sanitized templates, campaign eligibility review and approval snapshots, a deterministic database outbox worker, reports, exports, and manual data deletion. The frontend production build and Python syntax compilation pass. Backend runtime/migration verification remains outstanding. Real email sending, provider webhooks, and paid services are disabled. Do not use this application to send real email.

## Stack

- UI: React, TypeScript, Vite, Tailwind CSS
- API: Python, FastAPI, Pydantic settings
- Persistence: PostgreSQL via SQLAlchemy/Alembic (initial contact, consent, suppression, and import schema)
- Jobs: PostgreSQL outbox with local mock worker; Redis is reserved for future job orchestration
- Email: provider interface with local mock first; SES API/SMTP only in a later approved phase
- Local orchestration: Docker Compose

## Quick start

Prerequisites: Git, Docker Desktop with Compose, Node.js LTS, and Python 3.12+.

1. Copy `.env.example` to `.env`. Keep `EMAIL_PROVIDER=mock`.
2. Start local infrastructure: `docker compose up -d postgres redis`.
3. API: `cd backend`; create/activate a virtual environment; `python -m pip install -e ".[dev]"`; `uvicorn app.main:app --reload --port 8000`.
4. UI in a second terminal: `cd frontend`; `npm install`; `npm run dev`.
5. Open the Vite URL (normally `http://localhost:5173`) and API docs at `http://localhost:8000/docs`.

See [`SETUP_GUIDE.md`](SETUP_GUIDE.md) for local setup and [`WORKFLOW.md`](WORKFLOW.md) for operator safeguards.

Apply database migrations from `backend` with `alembic upgrade head`. `/health` reports process liveness; `/ready` returns HTTP 503 when PostgreSQL is unavailable.

After applying migrations, run the local mock outbox worker from `backend` with `python -m app.worker`; use `--once` to process currently due jobs and exit. It cannot send email and requires `EMAIL_PROVIDER=mock`.

Campaign reports are available in the operator dashboard. CSV exports are audited and protect spreadsheet cells that begin with formula characters.

See [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md) for a local database backup and isolated restore drill. Production startup validation requires `APP_ENV=production`, `DEBUG=false`, unique 32+ character `SECRET_KEY` and `UNSUBSCRIBE_SIGNING_KEY`, `OPERATOR_PASSWORD_HASH`, and `SECURE_COOKIES=true`; live provider support remains disabled.

Contact and campaign event retention is manual with no automatic expiry. Operators can delete contacts and campaign event timelines in the dashboard; matching global suppression remains to block re-import.

Operator access has no signup route. Before using `/auth/login`, set `OPERATOR_PASSWORD_HASH` to a hash produced by `pwdlib` (for example, from a local Python shell with `PasswordHash.recommended().hash("your password")`). Set `SECURE_COOKIES=true` behind HTTPS. The login response returns a CSRF token; send it as `X-CSRF-Token` on state-changing authenticated requests. The session cookie is HttpOnly and server-side session records expire after `SESSION_MAX_AGE_SECONDS`.

## Documentation

- [`PRD.md`](PRD.md): product requirements
- [`Brain.md`](Brain.md): purpose, principles, assumptions, decisions
- [`Design.md`](Design.md) and [`Designs.md`](Designs.md): UX principles and screen specifications
- [`Development.md`](Development.md): codebase and engineering sequence
- [`Task.md`](Task.md) and [`Micro Tasks.md`](Micro%20Tasks.md): milestone backlog and small tasks
- [`Testing.md`](Testing.md) and [`tRD.md`](tRD.md): verification and provisional readiness definition
- [`Production Checklist.md`](Production%20Checklist.md): production release gates
- [`Rules.md`](Rules.md): product and engineering rules
- [`Memory.md`](Memory.md): durable project context
- [`PROJECT_PLAN.md`](PROJECT_PLAN.md): requirements and development phases
- [`ARCHITECTURE.md`](ARCHITECTURE.md): component and data architecture
- [`SETUP_GUIDE.md`](SETUP_GUIDE.md): local development setup
- [`LOCAL_DATA_RESET.md`](LOCAL_DATA_RESET.md): destructive reset for local synthetic data
- [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md): local backup and isolated restore drill
- [`WORKFLOW.md`](WORKFLOW.md): operator workflow and safeguards
- [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md): current API and planned endpoints
- [`DEPLOYMENT.md`](DEPLOYMENT.md): production readiness and deployment plan
- [`SES_SETUP.md`](SES_SETUP.md): future SES setup checklist; no AWS resources are configured

## Safety rules

- Import only contacts with a documented lawful basis and permission appropriate to the campaign.
- Require consent source/time and list ownership before a contact can enter an eligible audience.
- Apply unsubscribe and suppression checks immediately before enqueue and before provider submission.
- Process hard bounces and complaints into suppression; pause campaigns when provider thresholds or quota controls are reached.
- Include sender identity and a working unsubscribe method in every marketing message.
- Never attempt spam-filter bypass, evasion, purchased-list delivery, or inbox-placement guarantees.
- Never commit credentials, contact exports, or real message content. Keep provider secrets out of the browser.

## “Ponytail skills” note

The referenced conversation asks to inspect this phrase rather than assume what it means. The installed skill inventory and workspace scan found no skill or project file called “ponytail” or “pony-tail”. The intended skill remains unidentified; no skill-specific workflow was applied.
