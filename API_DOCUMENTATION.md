# API documentation

## Current API

Base URL locally: `http://localhost:8000`. OpenAPI UI: `/docs`; schema: `/openapi.json`.

### System routes

- `GET /health`: process liveness.
- `GET /ready`: database readiness; returns `503` while PostgreSQL is unavailable.
- `GET /system/alerts`: real-time operational monitoring; flags queue lag (> 5 minutes overdue), high bounce rate (> 5.0% in 24h), and high complaint rate (> 0.1% in 24h).
- Responses include no-store caching and baseline content/frame/referrer/permissions headers. HSTS is added when secure cookies are enabled.

### Operator authentication

- `POST /auth/login`: accepts username and password (plus optional `totp_code`). If TOTP MFA is configured (`OPERATOR_TOTP_SECRET`) and code is omitted, returns `{"mfa_required": true}`. With valid credentials and TOTP code, sets an HttpOnly server-side session cookie and returns a CSRF token. There is no signup route. Login is disabled until `OPERATOR_PASSWORD_HASH` is configured.
- `GET /auth/mfa/status`: returns `{"mfa_enabled": boolean}` indicating whether two-factor authentication is active on the server.
- `GET /auth/me`: returns the authenticated operator; requires the session cookie.
- `GET /auth/csrf`: returns the current session's CSRF token after a browser reload; requires the session cookie.
- `POST /auth/logout`: invalidates the session and clears the cookie; requires the session cookie and `X-CSRF-Token` header.
- `GET /audit-logs`: returns the most recent administrative and security audit events (`limit` parameter up to 500); requires an authenticated operator session.
- The API access middleware denies every nonpublic route without a valid session and requires `X-CSRF-Token` on mutations.

Authentication successes, failures, and logout actions are recorded in `audit_events`.

### Contact imports

- `POST /imports/preview`: multipart `file` plus `mapping_json`. The mapping must explicitly identify email, consent source, consent timestamp, consent purpose, and consent provenance columns; `display_name` is optional. Files are UTF-8 CSV, at most 1 MB, 5,000 rows, 100 columns, and 5,000 characters per cell. Preview returns row numbers/statuses, counts, a batch ID, and a one-time commit token. Contact values are staged server side and not echoed in the response.
- `POST /imports/{batch_id}/commit`: JSON `{ "commit_token": "..." }` with `X-CSRF-Token`. Token expires after 30 minutes and is consumed once. The server rechecks contact uniqueness and global suppression at commit; suppressed or existing addresses are skipped and import never removes suppression.
- `GET /contacts`: list contacts and consent-record counts for the authenticated operator.
- `DELETE /contacts/{contact_id}`: manually delete contact and consent data, redact that address from approved snapshots/outbox history, and invalidate staged previews containing it. A matching global suppression remains in force.

### Suppression and unsubscribe

- `GET /suppressions`, `POST /suppressions`: inspect global suppression entries and add an idempotent manual block. Suppressions cannot be removed through the API.
- `GET /unsubscribe/{token}`: confirmation page; does not change state.
- `POST /unsubscribe/{token}`: verifies the stable signed campaign/contact token and applies global suppression idempotently. The unsubscribe signing key must remain stable across deployments.

### Email templates

- `GET /templates`: list saved operator templates.
- `POST /templates`: create a template; `PUT /templates/{template_id}` updates one.
- `DELETE /templates/{template_id}`: delete a saved template; action is audited.
- `POST /templates/preview`: validates and returns a sanitized HTML/plain text preview. HTML removes scripts, forms, event handlers, and unsafe URL schemes. Both bodies must contain exactly one `{{unsubscribe_url}}` placeholder. Sender identity and mailing address are required.

### Campaign review

- `GET /campaigns`, `POST /campaigns`: list campaigns and create a draft using a saved template and exact consent purpose.
- `GET /campaigns/{campaign_id}/eligibility`: preview eligible contacts and exclusion reasons. Contact email addresses are returned only to the authenticated operator.
- `POST /campaigns/{campaign_id}/approve`: recalculates eligibility and suppression, enforces `MAX_CAMPAIGN_RECIPIENTS`, then freezes template content and the eligible recipient snapshot. Empty or over-limit audiences cannot be approved.
- `POST /campaigns/{campaign_id}/pause`, `/resume`, `/cancel`: audited lifecycle transitions. Pausing leaves queued jobs pending until resume; cancelling marks pending jobs failed with a cancellation reason. Queue actions run only the local mock worker; there is no external sending path.
- `POST /campaigns/{campaign_id}/enqueue-mock`: idempotently adds approved snapshot recipients to the local database outbox.
- `GET /campaigns/{campaign_id}/events`: returns the deduplicated campaign event timeline.
- `DELETE /campaigns/{campaign_id}/events`: manually deletes the timeline and audits the deletion separately.
- `GET /dashboard/summary`: authenticated totals for approved campaigns, unsuppressed contacts with consent evidence, accepted mock messages, and global suppressions. Purpose-specific campaign eligibility still requires review.
- `GET /campaigns/{campaign_id}/report`: aggregate outbox metrics with accepted and delivered reported separately.
- `GET /campaigns/{campaign_id}/export.csv`: audited recipient outcome export; spreadsheet formula prefixes are neutralized and response caching is disabled.

### Provider webhooks

- `POST /webhooks/provider`: Ingests asynchronous provider events (bounce, complaint, delivered, accepted, unsubscribe) with HMAC-SHA256 signature verification.
  - Requires `X-Provider-Signature` or `X-Signature-SHA256` computed with `WEBHOOK_SIGNING_SECRET`.
  - Rejects unsigned or forged requests with HTTP 401 and audits the rejection; no database state is modified.
  - Replay-protected and idempotent: deduplicates by `event_id` so duplicate webhook deliveries are safely ignored.
  - Hard bounce and complaint events automatically create global suppression entries and transition targeted outbox messages to terminal `FAILED`.
  - Unsubscribe events add global suppression.
  - Delivered and accepted events update outbox message status and record auditable campaign events.

The mock worker runs from `backend` with `python -m app.worker` (continuous polling) or `python -m app.worker --once` (drain currently due jobs). It rechecks campaign status, exact consent evidence, contact identity, and global suppression immediately before each outcome. Transient mock outcomes retry with bounded exponential backoff up to three attempts. Deterministic outcomes distinguish accepted, delivered, hard bounce, and complaint. Bounce and complaint outcomes add global suppression. The worker enforces `MAX_SENDS_PER_MINUTE` rate caps by pausing without dropping pending recipients. The worker refuses to start unless `EMAIL_PROVIDER=mock`.

## Planned API surface

The planned core routes are now implemented.

| Area | Implemented routes | Notes |
|---|---|---|
| Operator | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `GET /auth/csrf` | Implemented; single operator, rate-limited, no public registration |
| Contacts | `GET /contacts`, `DELETE /contacts/{id}` | Manual deletion redacts history; consent evidence required on intake |
| Imports | `POST /imports/preview`, `POST /imports/{batch_id}/commit` | Explicit consent mapping, expiring preview, suppression recheck |
| Campaign review | `GET/POST /campaigns`, eligibility, approve, pause/resume/cancel, enqueue-mock | Approval freezes content and recipients; local mock outbox |
| Suppression | `GET/POST /suppressions`, `GET/POST /unsubscribe/{token}` | Idempotent global block; signed stable token |
| Templates | `GET/POST /templates`, `PUT/DELETE /templates/{id}`, preview | Sanitized HTML/text; required unsubscribe placeholder |
| Reports | `GET /campaigns/{id}/report`, event timeline/delete, CSV export | Accepted and delivered shown separately; formula-safe export |
| Provider webhooks | `POST /webhooks/provider` | HMAC-SHA256 signature verification, replay protection, bounce/complaint/unsub ingestion |

## API rules

- Normalize email addresses consistently; avoid leaking whether an address exists through unauthenticated endpoints.
- Require auth and CSRF protection for operator mutations.
- Apply file size/row limits to imports and per-IP request caps to login and unsubscribe.
- Do not accept provider credentials from frontend code or return secrets in responses.
- Validate campaign state transitions server-side and record an audit event.
- Recheck consent and suppression immediately before handing a message to a provider.
