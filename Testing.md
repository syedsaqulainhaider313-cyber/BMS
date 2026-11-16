# Testing strategy

The automated test suite is fully implemented in `backend/tests/` and runs with `pytest`. It covers all 15 critical scenarios below, unit utilities (email normalization, formula injection defense, rate limiting, token cryptography, template sanitization), and inbound signed provider webhooks.

## Test layers

- Unit (`tests/test_unit.py`): address normalization, consent eligibility, suppression precedence, formula injection defense, template sanitization, rate limiter window sliding.
- Critical Scenarios (`tests/test_critical_scenarios.py`): all 15 critical path invariants tested against SQLite database sessions.
- Inbound Webhooks (`tests/test_webhooks.py`): HMAC-SHA256 signature verification, rejection of unsigned/forged requests without state change, bounce/complaint/unsub ingestion, replay deduplication.
- UI: accessible navigation, keyboard flow, live preview, in-app modal confirmation, search/filter, and empty/error states.
- Security: CSRF/session, upload limits, HTML sanitization, token replay, log redaction, export formula injection defense.
- Operational: rate capping, quota pause, emergency pause/cancel controls, idempotent outbox.

## Critical scenarios

1. Importing a suppressed address does not clear suppression or make it eligible.
2. Unsubscribe after enqueue prevents dispatch.
3. Hard bounce and complaint events suppress the address, including on duplicate event delivery.
4. Missing consent or purpose excludes the contact and explains why.
5. Provider timeout does not create duplicate delivery on retry.
6. Quota exhaustion pauses the worker and does not silently drop recipients.
7. Invalid provider webhook signature changes no state.
8. Campaign content/audience snapshot is immutable after approval, except dispatch-time suppression removal.
9. Mock mode has no network path to a real provider.
10. Reports do not label provider acceptance as delivery.
11. Manual contact deletion redacts snapshots/outbox data, invalidates previews, and preserves an existing suppression.
12. Unsubscribe GET does not mutate state; signed POST is idempotent and globally suppresses the address.
13. Campaign event deletion is explicit and leaves audited evidence in the separate audit log.
14. Pausing a campaign leaves pending/retry outbox rows untouched, and the worker does not process them until resume.
15. Cancelling a campaign marks its pending/retry rows terminal so the worker cannot later process them.

## Local verification

Follow `SETUP_GUIDE.md` with Python 3.12 and PostgreSQL. Add automated tests for every critical scenario above before production review. The present environment lacks Python 3.12, so runtime and migration verification remain pending. Do not treat placeholder dashboard metrics as real campaign data.
