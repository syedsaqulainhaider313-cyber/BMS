# Development guide

## Repository layout

```text
backend/                 FastAPI service
  app/                   API, settings, persistence models, and operator auth
frontend/                React + Vite + Tailwind UI
  src/                   application shell
docker-compose.yml       local PostgreSQL and Redis
*.md                     project requirements and working guides
```

Planned additions: webhook verification, operational alerts, automated production backups, a deletion/retention review, provider adapters only after separate approval, and automated tests.

## Local development

Follow `SETUP_GUIDE.md`. Run UI and API separately; Docker Compose is only for local PostgreSQL/Redis. `EMAIL_PROVIDER=mock` is mandatory until a separately approved provider phase. Do not use real contact data during initial UI work.

## Engineering sequence

1. Verify implemented migrations, auth, import, template, campaign, mock worker, report, and unsubscribe paths under Python 3.12 and PostgreSQL.
2. Add automated critical-path/security tests and CI after repository ownership and conventions are settled.
3. Complete provider webhook verification, queue alerts, production backup automation, and a restore drill.
4. Reconfirm privacy/legal requirements and review the suppression exception to manual contact deletion.
5. Complete the production checklist; a paid provider remains a separate explicit approval.

## Change expectations

- Keep API contracts documented in `API_DOCUMENTATION.md`.
- Update the relevant requirements, architecture, workflow, and task docs when behavior changes.
- Keep secrets, contact files, and exports out of source control.
- Prefer small, reviewable changes with explicit failure behavior.
- Do not add live sending paths while mock-only phase is active.
