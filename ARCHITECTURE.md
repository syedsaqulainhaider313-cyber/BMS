# Architecture

## System boundary

```text
Windows browser
    │ HTTPS (localhost in development)
React + Vite + Tailwind UI
    │ JSON API; secure operator session
FastAPI application ───────── PostgreSQL
    │                           contacts, consent, suppression,
    │                           campaigns, snapshots, events, audit
    └── Redis queue ── Celery worker ── Provider interface
                                      └── Local mock only (phase 0)
                                           Future: SES API/SMTP after approval
```

The browser never sends email directly and never receives provider credentials. The API is the authority for campaign eligibility and suppression. Workers repeat the eligibility check at dispatch time, because a contact may unsubscribe after a campaign is queued.

## Planned modules

- `contacts`: normalized address, client label, consent evidence, import provenance
- `suppression`: global address-level suppression and immutable reason/history
- `campaigns`: draft/review/approved/queued/paused/completed states; recipient snapshot
- `templates`: sanitized HTML and plain-text alternative, required sender/footer fields
- `sending`: provider protocol, quota/rate control, idempotent outbox, retry and pause
- `events`: signed provider webhook intake, deduplication, bounce/complaint/unsubscribe effects
- `audit`: operator actions and security-relevant changes
- `reporting`: derived counts from events, with acceptance distinct from delivery

## Core entities (planned)

`Contact`, `ConsentRecord`, `Suppression`, `ImportBatch`, `Template`, `Campaign`, `CampaignRecipient`, `MessageAttempt`, `ProviderEvent`, `AuditEvent`, and `ProviderConfiguration`. Consent records are append-only evidence. Suppression is global and takes precedence over contact status and campaign snapshots. Provider configuration stores a secret reference, never a plaintext credential in a database row or API response.

## Sending invariants

1. Eligible recipient requires valid address, active contact, documented consent for the purpose, and no global suppression.
2. Eligibility is checked at preview, enqueue, and immediately before provider submission.
3. Campaign launch freezes its content and recipient snapshot; suppression can still remove a recipient at dispatch.
4. Idempotency key is unique per campaign recipient and provider attempt policy prevents duplicate submission after uncertain outcomes.
5. Retry transient failures with capped exponential backoff; do not retry permanent rejection, hard bounce, complaint, or policy violation.
6. Quota/rate cap or provider health failure pauses queue dispatch. Operator can pause/cancel at any time.
7. Provider events are signature-checked, deduplicated, auditable, and applied idempotently.

## Local/production distinction

Local development uses Docker Compose for PostgreSQL and Redis, mock adapter, and development-only secrets. Production is a later deployment decision. It requires HTTPS, secure secret storage, managed database/Redis or hardened equivalents, backups, monitoring, webhook verification, access restrictions, and a tested restore plan. Do not expose the development server to the public internet.
