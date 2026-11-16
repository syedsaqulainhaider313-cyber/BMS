# Designs and screen specifications

This document describes the planned interaction design. It complements `Design.md`, which contains visual and accessibility principles.

## Navigation

Overview · Contacts · Templates · Campaigns · Reports · Settings

Navigation is for one authenticated operator. There is no organization switcher, tenant signup, or client login in the current scope. Client/work labels are record filters only.

## Screens

### Overview

- Active/paused campaigns, eligible contacts, accepted messages, and suppressed contacts.
- Recent activity and next-step checklist.
- Persistent environment badge: Local/mock or production provider status.
- Clear mode notice: real sending disabled while provider is mock.

### Contacts and imports

- Import wizard: choose CSV, map fields, preview rows, show invalid/duplicate/suppressed counts, then commit.
- Consent evidence fields are explicit and required for campaign eligibility; import never creates consent by default.
- Contact detail shows source, purpose, timestamp, status, and suppression reason.
- Global suppression record is visible but cannot be cleared through ordinary import/update.

### Templates

- Separate subject, sender identity, HTML, and plain-text fields.
- Preview desktop/mobile and render personalization with safe sample values.
- Required unsubscribe token and sender details are validated before saving as campaign-ready.
- HTML is sanitized; preview rendering is isolated.

### Campaigns

- Draft → review → approved → scheduled/queued → sending → paused/completed/cancelled.
- Audience preview shows included and excluded counts with reasons.
- Review checklist includes purpose/permission, sender, content, unsubscribe, suppression, and rate cap.
- Confirmation names audience size and schedule; pause/cancel remains available.

### Reports

- Accepted, delivered (when provider supports it), deferred, failed, bounced, complained, and unsubscribed are separate states.
- Export is scoped and auditable; reports avoid exposing unnecessary personal data.

### Settings

- Provider state and quota caps; secrets remain masked and server-side.
- Mock provider is the only active provider in the initial version.
- Account/security, retention, and audit settings are future phases.

## Interaction states

All data views need loading, empty, error, and permission-denied states. Destructive or bulk operations require a clear summary and confirmation. Suppression and unsubscribe outcomes must be visible immediately. Never use a success state for a queued message as if it were delivered.
