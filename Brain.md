# Project brain

This document preserves the project's core context so future work starts from the same product direction.

## Purpose

Build a private bulk mail operations tool for one developer/operator. The operator handles campaign preparation and reporting for their work. This is not a multi-tenant SaaS and has no client-facing login in the current scope.

## Product principles

1. Permission before volume: a contact needs documented consent or another verified lawful basis appropriate to the specific campaign.
2. Suppression wins: unsubscribe, hard bounce, complaint, and manual suppression block dispatch globally, even after re-import.
3. Fail closed: if consent, suppression, quota, provider health, or event processing cannot be checked, do not send.
4. Human review: preview the audience and content, then explicitly approve and retain an audit trail.
5. Safe defaults: mock provider locally, low caps, no public registration, and no provider secrets in the browser.
6. Honest reporting: provider acceptance is not represented as inbox delivery.
7. No evasion: no spam-filter bypass, scraped/purchased lists, or inbox-placement promises.

## Confirmed direction

- Operator: one developer/operator
- Interface: responsive web application used from Windows
- UI: React + Vite + Tailwind CSS
- API: Python + FastAPI
- Persistence: PostgreSQL
- Background processing: Redis + Celery
- Initial email adapter: local mock only
- Possible future provider: Amazon SES API/SMTP, only after separate approval
- Paid services: deferred

## Open decisions

- Meaning of “ponytail skills”: unresolved; workspace and installed skills had no matching skill. Do not guess.
- Meaning of “tRD”: provisionally interpreted as test/readiness definition for this documentation set.
- Hosting region, domain, retention period, applicable jurisdictional policy, and provider: decide before production work.

## Decision log

| Decision | Status | Reason |
|---|---|---|
| Single-operator tool, not SaaS | Confirmed | Operator is also the developer and user |
| Browser UI + cloud-capable backend | Confirmed direction | Usable from Windows; workers can eventually run independently |
| Mock first, SES later | Confirmed | Build and review safeguards before paid sending |
| Permission-based use only | Required | Consent, opt-out, suppression, and provider health controls are core behavior |
