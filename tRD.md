# Test and readiness definition (provisional “tRD”)

> The user requested a document named “tRD” without expanding the acronym. For now this file interprets it as a test/readiness definition. Rename or revise it if a different meaning was intended.

## Readiness levels

### R0: Documentation and shell

- Project direction, constraints, setup, architecture, and roadmap are documented.
- UI and API shell can start locally.
- Provider setting is mock-only and no real send route exists.

### R1: Safe local feature development

- Database schema/migrations exist.
- Authentication, contact consent, and global suppression are implemented.
- Automated unit/API tests cover import and eligibility behavior.
- No real contact data required for local test runs.

### R2: Staging simulation

- Mock queue and worker are idempotent and restart-safe.
- Campaign approval snapshot, pause/cancel, quota controls, and event lifecycle pass integration tests.
- Unsubscribe/bounce/complaint events block pending dispatch.
- Synthetic data and observability are in place.

### R3: Production candidate

- Security review, backups/restore, retention, incident response, monitoring, and deployment rollback are complete.
- Provider event authentication and quota controls are verified.
- Production checklist has evidence for every applicable item.
- Operator explicitly approves provider, spend, region, and restricted pilot.

## No-go conditions

Any unknown consent basis, failed suppression check, unauthenticated provider event path, absent quota cap, insecure secret handling, untested pause path, or misleading delivery metric blocks progression. Mock-mode completion is not permission to enable a paid provider.
