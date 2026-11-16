# Project: Bulk Mail Management System (BMS) Scaling & Deliverability Audit

## Architecture Overview
The Bulk Mail Management System (BMS) is a privacy-first, compliance-focused transactional and bulk dispatch platform.
- **Current Core Stack**: FastAPI (Python 3.12+), SQLAlchemy 2.0 (Psycopg 3), PostgreSQL 16, Redis 7 (Compose), React/Vite SPA.
- **Architectural Scaling Target**: Support batches of 10,000 to 40,000 emails concurrently and sustain a daily volume of 100,000 inbox-delivered emails without database saturation, queue lag, or ISP spam quarantine.
- **Key Subsystems**:
  1. *Ingestion & Intake*: Contact management, CSV batch streaming, affirmative consent records.
  2. *Audience Qualification & Normalization*: Relational eligibility evaluation, SQL pushdown, suppression list filtering.
  3. *Distributed Queue & Worker Cluster*: Celery distributed workers with Redis broker, task chunking, prefetch limits, rate limiting.
  4. *Outbound Delivery Pipeline*: Amazon SES API with Dedicated IP vs Self-Hosted SMTP (PowerMTA/Postfix), RFC 8058 headers, DKIM signing, bounce/complaint webhooks.
  5. *Deliverability & Reputation Guard*: Redis Token Bucket rate limiting per ISP, automated circuit-breaker suppression, DMARC/BIMI alignment.
  6. *Infrastructure & Observability*: pgBouncer connection pooling, Prometheus metrics, structured PII-redacted logging.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Database Normalization & SQL Pushdown | Replace `recipient_snapshot` JSON column with relational table `campaign_recipients`; push eligibility to indexed SQL queries | M1 | survey_architecture, survey_tests_and_infra |
| 2 | Celery & Redis Distributed Queue Engine | Integrate Celery with Redis broker, replace single-threaded `worker.py` polling loop, configure task routing & prefetch | M1 | survey_architecture, survey_tests_and_infra |
| 3 | Database Connection Pooling & pgBouncer | Configure SQLAlchemy QueuePool and deploy pgBouncer in transaction pooling mode for multi-worker concurrency | M1 | survey_tests_and_infra |
| 4 | Batch Scaling & Infrastructure Sizing (10k-40k, 100k/day) | Bottleneck analysis, concurrency limits, and exact hardware specs (CPU, RAM, workers, IOPS) across 10k, 20k, 30k, 40k tiers | M1 | ORIGINAL_REQUEST §R1 |
| 5 | Amazon SES vs Self-Hosted SMTP Comparison | Deep comparative analysis covering cost ($/mo), reputation isolation, operational overhead, and deliverability control | M2 | ORIGINAL_REQUEST §R2, survey_deliverability |
| 6 | Email Authentication Protocols (SPF, DKIM, DMARC, BIMI) | Technical specifications, custom Return-Path domain (`bounces.domain.com`), DKIM 2048-bit signing, DMARC `p=quarantine/reject`, BIMI | M2 | ORIGINAL_REQUEST §R2, survey_deliverability |
| 7 | Deliverability Compliance (RFC 8058 One-Click Unsubscribe) | Implementation of `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, RFC 5322 MIME builder, headers | M2 | ORIGINAL_REQUEST §R2, survey_deliverability |
| 8 | ISP Throttling & Redis Rate Limiting | Domain-specific token bucket throttling for Gmail, Yahoo, Microsoft; eliminate PostgreSQL aggregate rate-query bottleneck | M2 | ORIGINAL_REQUEST §R2, survey_architecture, survey_deliverability |
| 9 | Bounce/Complaint Webhooks & Reputation Guard | Amazon SNS webhook adapter, distinction of transient soft bounces (4xx) from permanent hard bounces (5xx), circuit breakers (<0.1% complaint, <2% bounce) | M2 | ORIGINAL_REQUEST §R2, survey_deliverability |
| 10 | 30-Day IP & Domain Warm-Up Schedule | Daily volume escalation matrix, hourly send caps, domain-split schedule, seed list monitoring for 100k/day ramp | M2 | ORIGINAL_REQUEST §R2, survey_deliverability |
| 11 | Commercial Bulk Tool Pitfalls vs BMS Inbox Architecture | Technical autopsy of commercial mass-mail spam drivers vs BMS cryptographic and architectural inbox placement | M2 | ORIGINAL_REQUEST §R2 |
| 12 | Prioritized Technical Implementation Roadmap | Phase-by-phase engineering execution plan detailing code changes, config updates, file modifications, and deployment steps | M3 | ORIGINAL_REQUEST §R3 |
| 13 | Operator Architectural Trade-Off Analysis | Comprehensive trade-off matrices for SES vs SMTP, Celery vs Outbox, JSON vs Relational snapshots, Celery Beat vs Redis limits | M3 | ORIGINAL_REQUEST §R3 |
| 14 | Verification, Compliance & Test Suite Evaluation | RFC compliance test matrix, Google/Yahoo Feb 2024+ validation procedures, integration tests against PostgreSQL & Celery | M3 | ORIGINAL_REQUEST §R4, survey_tests_and_infra |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Architectural Scaling & Batch Capacity Audit | Assess bottlenecks for 10k, 20k, 30k, 40k batches and 100k/day; database pooling & pgBouncer; Celery/Redis queue sizing; hardware/worker configuration | Features 1, 2, 3, 4 | **DONE** (`scaling_capacity_audit.md`) |
| M2 | Deliverability, Anti-Spam Architecture & Provider Comparison | Commercial tool spam autopsy; SES vs Self-Hosted SMTP; SPF/DKIM/DMARC/BIMI; RFC 8058 headers; ISP throttling; bounce/complaint handling; 30-day warm-up | Features 5, 6, 7, 8, 9, 10, 11 | **DONE** (`deliverability_anti_spam_audit.md`) |
| M3 | Actionable Implementation Roadmap & Verification Framework | Prioritized step-by-step engineering roadmap; operator trade-off analysis; verification procedures against RFCs & Google/Yahoo 2024; test suite modernization | Features 12, 13, 14 | **DONE** (`implementation_roadmap_and_verification.md`, 61 tests passing) |

## Interface Contracts

### Ingestion ↔ Relational Campaign Recipients
- **Input**: `campaign_id: UUID`, `segment_filters: dict`
- **Output**: Relational entries in `campaign_recipients` populated via `INSERT INTO campaign_recipients (campaign_id, contact_id, status) SELECT ... FROM contacts WHERE ... AND NOT EXISTS (SELECT 1 FROM suppressions ...)`
- **Constraint**: O(1) memory footprint in FastAPI ASGI workers.

### Dispatcher ↔ Celery Task Queue
- **Task Signature**: `dispatch_campaign_batch(campaign_id: str, recipient_batch_ids: list[int]) -> dict`
- **Routing**: `bulk_dispatch` queue, Redis broker with `worker_prefetch_multiplier=1`, `task_acks_late=True`
- **Rate Limit Contract**: Checks ISP-specific Redis Token Bucket (`ratelimit:{isp}:{timestamp_minute}`) before invoking provider.

### Delivery Provider ↔ Outbound Gateway (SES / SMTP)
- **Interface**: `EmailProvider.dispatch_batch(messages: list[OutboundMessage]) -> list[DispatchResult]`
- **Headers Contract**: RFC 5322 compliance, `List-Unsubscribe: <https://.../unsubscribe/one-click/{token}>, <mailto:unsubscribe@...>`, `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, DKIM 2048-bit `rsa-sha256` signature.

### Inbound Events ↔ Webhook Receiver & Reputation Circuit Breaker
- **Endpoint**: `/webhooks/events/{provider}`
- **Payload Contract**: Amazon SNS JSON notification payload parsing (`notificationType in ["Bounce", "Complaint", "Delivery"]`).
- **Suppression Invariant**: Hard bounce (`bounceType == "Permanent"`) -> Immediate global suppression. Soft bounce (`bounceType == "Transient"`) -> Retry with exponential backoff; suppress only after 3 consecutive failures within 72 hours. Complaint -> Immediate permanent suppression + automated alert.
- **Circuit Breaker**: If 24h complaint rate > 0.1% or bounce rate > 2.0%, set `campaign_dispatch_halted = True` in Redis and pause Celery workers.

## Code Layout
```
c:\Users\Asus\Desktop\BMS-Project\
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI application entrypoint & API routes
│   │   ├── models.py             # SQLAlchemy 2.0 ORM models
│   │   ├── settings.py           # Pydantic v2 application settings
│   │   ├── db.py                 # SQLAlchemy engine & session maker
│   │   ├── worker.py             # Outbox worker (to be refactored to Celery tasks)
│   │   ├── tasks/                # New Celery tasks package
│   │   │   ├── celery_app.py     # Celery configuration & Redis broker
│   │   │   ├── dispatch.py       # Chunked email dispatch tasks
│   │   │   └── hygiene.py        # List hygiene, suppression sync, alert sweeps
│   │   ├── provider.py           # Provider abstractions (Mock, Amazon SES, SMTP)
│   │   ├── templates.py          # Email HTML sanitization & rendering engine
│   │   ├── throttling.py         # Redis Token Bucket ISP rate limiter
│   │   ├── security.py           # Authentication, MFA, HMAC verification
│   │   └── webhooks/             # Inbound webhook adapters (SES/SNS, SendGrid)
│   ├── scripts/                  # Operational tools, drills, diagnostics
│   └── tests/                    # Pytest test suite (unit, integration, load)
├── docker-compose.yml            # Local development orchestration (App, Postgres, Redis)
├── docker-compose.prod.yml       # Production orchestration (App, Worker, Postgres, Redis, pgBouncer)
└── docs/                         # Architecture, compliance, audit documentation
```
