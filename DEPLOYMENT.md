# Deployment plan

Production deployment is out of scope for the initial scaffold. Do not expose the development server publicly or connect live email credentials yet.

The current application still supports only the local mock provider. Production startup validation rejects debug mode, weak/default secret keys, missing operator password hashes, and insecure cookies; API docs are disabled in production mode. This is configuration hardening only and does not approve or make live sending available. Local Compose database ports bind to loopback. See [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md) for the local dump and isolated restore drill; production storage/retention still needs an operator-approved design.

## Before any production release

- Complete application phases for authentication, data model, consent evidence, suppression, queued delivery, events, and reporting.
- Complete provider webhook signature/replay handling, operator MFA, retention/deletion, and integration test coverage.
- Decide data residency, jurisdiction, retention, deletion, backup, and incident procedures.
- Use a supported production hosting target with private network boundaries; no public database or Redis.
- Terminate HTTPS and configure secure cookies, CSRF defenses, operator MFA, access logging, and secret management.
- Separate development/staging/production credentials and databases.
- Set database encryption, automated backups, point-in-time recovery where available, and perform a restore drill.
- Add worker health, queue depth, provider quota, bounce, complaint, and webhook-lag alerts. Redact addresses and message content from logs.
- Verify provider webhook signatures and replay protection; test pause/circuit-break behavior.
- Apply dependency scanning, migrations review, deployment rollback, and security review.
- Set explicit sending caps and spend caps. Start with a small operator-approved test audience of consenting recipients.

## Release sequence

1. Local mock-only development.
2. Staging with synthetic contacts and deterministic mock outcomes.
3. Security, restore, and unsubscribe/suppression review.
4. Separate approval to choose and purchase/enable production services.
5. Provider integration in staging, event verification, quota controls, then restricted production pilot.

## Containerized Production Deployment

A production-hardened multi-stage `Dockerfile` and `docker-compose.prod.yml` are provided.

### Characteristics:
1. **Multi-Stage Build**: Node 20 compiles the React application into `frontend/dist`; Python 3.12 slim runs the application with an unprivileged system user (`bms:bms`, UID 10001).
2. **Private Network Isolation**: PostgreSQL and Redis run on an isolated bridge network with zero published host ports.
3. **Secret Injection**: Secrets are loaded from mounted Docker secrets files in `/run/secrets/` via `*_FILE` settings variables, preventing credentials from appearing in environment variables or process listings.

### Launch Procedure:

1. **Generate Production Secrets**:
   ```bash
   python backend/scripts/generate_prod_secrets.py --output-dir ./secrets
   ```
   *Note: Save the printed operator credentials in your secure password manager.*

2. **Start Production Stack**:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```

3. **Run Database Migrations**:
   ```bash
   docker compose -f docker-compose.prod.yml exec web alembic upgrade head
   ```

4. **Verify Health**:
   ```bash
   curl -f http://127.0.0.1:8000/health
   curl -f http://127.0.0.1:8000/ready
   ```

Never deploy with `DEBUG` enabled, development keys, public docs endpoints, open admin routes, or a mock setting that silently falls through to a real provider.
