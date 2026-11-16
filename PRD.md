# Product requirements document

## Summary

Bulk Mail Management System (BMS) is an internal web application for one operator to prepare, review, execute, and report on permission-based email campaigns. The first release is local and mock-only.

## Problem

The operator needs a controlled workflow for handling client/work contact lists, consent provenance, campaign content, sending operations, and outcome reporting without losing unsubscribe or delivery-issue history across imports.

## Users

- Primary: the developer/operator, who manages lists, campaigns, and reports.
- No client accounts, public signup, or tenant administration in current scope.

## Goals

- Keep evidence for why an address is eligible for a particular campaign.
- Prevent suppressed addresses from being queued or sent, including after re-import.
- Make campaign review, pause/cancel, and reporting auditable.
- Keep providers replaceable and default to mock locally.

## Out of scope

- Multi-tenant SaaS or client self-service.
- Purchased/scraped-list marketing, cold outreach automation, spam-filter bypass, or inbox guarantees.
- Live SES configuration, paid purchases, or production sending in the initial scaffold.
- AI-generated audience sourcing or automatic consent inference.

## User stories

1. As the operator, I can import a CSV, map fields, and see invalid, duplicate, and suppressed rows before commit.
2. As the operator, I can inspect consent source, timestamp, purpose, and provenance for each contact.
3. As the operator, I can preview a campaign and understand exactly who is eligible and why others are excluded.
4. As the operator, I can require sender identity and unsubscribe details before campaign approval.
5. As the operator, I can pause/cancel work and see mock outcomes without sending real mail.
6. As the operator, I can review bounces, complaints, unsubscribes, and acceptance separately.

## Functional requirements

See `PROJECT_PLAN.md` for detailed numbered requirements. Core release gates: single-operator access, consent-aware import, global suppression, safe template preview, campaign approval snapshot, dispatch-time recheck, quota-controlled queue, idempotent event handling, and honest reporting.

## Success measures

- No campaign can dispatch without an explicit review/approval action.
- A suppressed address is excluded at import, preview, enqueue, and dispatch.
- Every eligible recipient has campaign-purpose consent evidence or an explicitly reviewed lawful basis.
- Every action affecting data, approval, provider, or campaign state has an audit record.
- Local mode cannot submit to an external email provider.

## Risks and dependencies

- Legal requirements vary by sender, audience, purpose, and jurisdiction; define policy before production.
- Provider reputation and quotas require staged, permission-based operations.
- Contact data is sensitive; retention, access, backup, and incident processes must be defined.
- Production depends on completed authentication, event processing, security review, and later provider approval.
