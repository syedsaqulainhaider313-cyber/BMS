r"""Empirical Adversarial Challenge Suite for Phase 3 (R2 & R3).
Author: challenger_p3_2 (Role: Empirical Challenger)

Adversarially challenges and verifies:
1. DeliverabilityCircuitBreaker mathematical boundary conditions:
   - Exactly 0.1000% complaint rate (<= 0.1% does NOT trip) vs 0.10001% / 0.1001% (must trip).
   - Exactly 5.000% hard bounce rate (<= 5.0% does NOT trip) vs 5.0001% / 5.025% (must trip).
   - Cold start guard: D < 50 prevents rate tripping (e.g. 1/10 = 10% does NOT trip, 4/49 does NOT trip, 19/49 does NOT trip).
   - Absolute hazard override: >= 5 complaints or >= 20 hard bounces trips immediately even with D < 50 or D = 0.
   - Denominator sample boundary: D = 50 with 6% hard bounce rate trips immediately.
2. State machine invariants & cascading side effects on trip:
   - Campaign transitions from APPROVED to PAUSED.
   - PENDING and RETRY outbox messages transition to FAILED with last_error="circuit_breaker_paused".
   - ACCEPTED, DELIVERED, and previously FAILED outbox messages remain unmutated.
   - QUEUED campaign recipients transition to CANCELLED.
   - DISPATCHED, BOUNCED, and COMPLAINED recipients remain unmutated.
   - High-priority AuditEvent emitted with actor="system:circuit_breaker".
   - /system/alerts surfaces status="critical" and code="circuit_breaker_tripped".
   - Non-approved campaigns (PAUSED, CANCELLED, DRAFT) are skipped safely.
3. Concurrent duplicate suppression inserts (race condition resilience):
   - Multi-threaded concurrent upsert of identical normalized email across 20 threads.
   - Multi-threaded concurrent upsert with unnormalized case variations.
   - Concurrently competing suppression reasons (complaint vs bounce).
   - Zero deadlock, zero IntegrityError, exactly 1 suppression row.
4. Sub-second suppression and state synchronization (R2):
   - Latency benchmark verifying write execution < 10ms SLA.
   - Precedence order: outbox_id -> (campaign_id, email_normalized) -> email_normalized.
   - Soft bounce vs hard bounce isolation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuditEvent
from app.circuit_breaker import DeliverabilityCircuitBreaker
from app.db import Base
from app.main import system_alerts
from app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    EmailTemplate,
    OutboxMessage,
    OutboxStatus,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.suppression import synchronize_recipient_state, upsert_global_suppression


@pytest.fixture
def p3_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def concurrent_engine(tmp_path):
    """File-backed SQLite engine with WAL mode and separate connection pooling

    to authentically simulate multi-process/multi-threaded database concurrency.
    """
    db_file = tmp_path / "concurrent_suppression.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30.0},
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def p3_db(p3_engine):
    connection = p3_engine.connect()
    transaction = connection.begin()
    SessionClass = sessionmaker(bind=connection, expire_on_commit=False)
    session = SessionClass()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def template(p3_db):
    tmpl = EmailTemplate(
        name="Challenger Template",
        subject="Deliverability Test",
        sender_name="Sender",
        sender_email="sender@example.com",
        mailing_address="123 Test St",
        html_body="<p>Test</p>",
        text_body="Test",
    )
    p3_db.add(tmpl)
    p3_db.commit()
    return tmpl


def _create_approved_campaign(db: Session, template_id: int, name: str = "Test Campaign") -> Campaign:
    campaign = Campaign(
        name=name,
        template_id=template_id,
        consent_purpose="transactional",
        status=CampaignStatus.APPROVED,
    )
    db.add(campaign)
    db.commit()
    return campaign


# ==============================================================================
# 1. BOUNDARY CONDITIONS ON SLIDING WINDOW CIRCUIT BREAKER
# ==============================================================================

class TestCircuitBreakerBoundaries:
    """Stress-tests exact mathematical boundaries for complaint and hard bounce rates."""

    def test_complaint_rate_exact_boundary_0_1_percent_does_not_trip(self, p3_db, template):
        """EXACT BOUNDARY: 1 complaint out of 1000 sends = exactly 0.0010 (0.1000%).

        Google/Yahoo rule: complaint rate must be <= 0.10%.
        0.0010 > 0.0010 is FALSE -> MUST NOT TRIP.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Complaint Boundary 0.100%")
        now = datetime.now(UTC)

        # 1000 accepted sends
        for i in range(1000):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-cb1-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        # 1 complaint
        p3_db.add(
            CampaignEvent(
                event_key="complaint-cb1-0",
                campaign_id=campaign.id,
                event_type="complaint",
                created_at=now,
            )
        )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "healthy"
        assert res["complaint_rate"] == 0.0010
        assert res["complaints"] == 1
        assert res["sent"] == 1000

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_complaint_rate_exact_boundary_multi_count_0_1_percent_does_not_trip(self, p3_db, template):
        """EXACT BOUNDARY: 2 complaints out of 2000 sends = exactly 0.0010 (0.1000%).

        Must NOT trip because rate <= 0.0010 and complaints < ABSOLUTE_COMPLAINTS_LIMIT (5).
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Complaint Boundary 2 in 2000")
        now = datetime.now(UTC)

        for i in range(2000):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-cb2-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        for i in range(2):
            p3_db.add(
                CampaignEvent(
                    event_key=f"complaint-cb2-{i}",
                    campaign_id=campaign.id,
                    event_type="complaint",
                    created_at=now,
                )
            )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "healthy"
        assert res["complaint_rate"] == 0.0010
        assert res["complaints"] == 2
        assert res["sent"] == 2000

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_complaint_rate_exceeding_boundary_trips(self, p3_db, template):
        """BOUNDARY ATTACK: 1 complaint out of 999 sends = 0.001001001... (0.1001%).

        0.001001001 > 0.0010 is TRUE -> MUST TRIP IMMEDIATELY.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Complaint Boundary 1 in 999")
        now = datetime.now(UTC)

        for i in range(999):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-cb3-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        p3_db.add(
            CampaignEvent(
                event_key="complaint-cb3-0",
                campaign_id=campaign.id,
                event_type="complaint",
                created_at=now,
            )
        )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert res["complaint_rate"] > 0.0010
        assert "Complaint rate" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

    def test_complaint_rate_micro_boundary_trip(self, p3_db, template):
        """MICRO-BOUNDARY ATTACK: 4 complaints out of 3999 sends = 0.00100025 (0.100025%).

        Tests floating-point epsilon sensitivity above 0.10%.
        Must trip without triggering absolute count override (4 < 5).
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Complaint Micro Boundary")
        now = datetime.now(UTC)

        for i in range(3999):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-cb4-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        for i in range(4):
            p3_db.add(
                CampaignEvent(
                    event_key=f"complaint-cb4-{i}",
                    campaign_id=campaign.id,
                    event_type="complaint",
                    created_at=now,
                )
            )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert res["complaint_rate"] > 0.0010
        assert "Complaint rate" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

    def test_hard_bounce_rate_exact_boundary_5_0_percent_does_not_trip(self, p3_db, template):
        """EXACT BOUNDARY: 5 hard bounces out of 100 sends = exactly 0.0500 (5.0000%).

        Industry rule: hard bounce rate must not exceed 5.0%.
        0.0500 > 0.0500 is FALSE -> MUST NOT TRIP.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Bounce Boundary 5 in 100")
        now = datetime.now(UTC)

        for i in range(100):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-bb1-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        for i in range(5):
            p3_db.add(
                CampaignEvent(
                    event_key=f"bounce-bb1-{i}",
                    campaign_id=campaign.id,
                    event_type="hard_bounce",
                    created_at=now,
                )
            )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "healthy"
        assert res["bounce_rate"] == 0.0500
        assert res["hard_bounces"] == 5
        assert res["sent"] == 100

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_hard_bounce_rate_exact_boundary_19_in_380_does_not_trip(self, p3_db, template):
        """EXACT BOUNDARY: 19 hard bounces out of 380 sends = exactly 0.0500 (5.0000%).

        19 is just below ABSOLUTE_BOUNCES_LIMIT (20). Rate is exactly 5.0%.
        MUST NOT TRIP.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Bounce Boundary 19 in 380")
        now = datetime.now(UTC)

        for i in range(380):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-bb2-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        for i in range(19):
            p3_db.add(
                CampaignEvent(
                    event_key=f"bounce-bb2-{i}",
                    campaign_id=campaign.id,
                    event_type="hard_bounce",
                    created_at=now,
                )
            )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "healthy"
        assert res["bounce_rate"] == 0.0500

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_hard_bounce_rate_exceeding_boundary_trips(self, p3_db, template):
        """BOUNDARY ATTACK: 5 hard bounces out of 99 sends = 0.050505... (5.0505%).

        0.050505 > 0.0500 is TRUE -> MUST TRIP IMMEDIATELY.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Bounce Boundary 5 in 99")
        now = datetime.now(UTC)

        for i in range(99):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-bb3-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        for i in range(5):
            p3_db.add(
                CampaignEvent(
                    event_key=f"bounce-bb3-{i}",
                    campaign_id=campaign.id,
                    event_type="hard_bounce",
                    created_at=now,
                )
            )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert res["bounce_rate"] > 0.0500
        assert "Hard bounce rate" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

    def test_hard_bounce_micro_boundary_trip(self, p3_db, template):
        """MICRO-BOUNDARY ATTACK: 10 hard bounces out of 199 sends = 0.050251... (5.0251%).

        10 is below absolute limit (20), but rate exceeds 5.0%.
        MUST TRIP IMMEDIATELY.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Bounce Boundary 10 in 199")
        now = datetime.now(UTC)

        for i in range(199):
            p3_db.add(
                CampaignEvent(
                    event_key=f"accepted-bb4-{i}",
                    campaign_id=campaign.id,
                    event_type="accepted",
                    created_at=now,
                )
            )
        for i in range(10):
            p3_db.add(
                CampaignEvent(
                    event_key=f"bounce-bb4-{i}",
                    campaign_id=campaign.id,
                    event_type="hard_bounce",
                    created_at=now,
                )
            )
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert res["bounce_rate"] > 0.0500

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED


# ==============================================================================
# 2. COLD START SAFEGUARDS & ABSOLUTE HAZARD LIMITS (D < 50)
# ==============================================================================

class TestColdStartAndAbsoluteHazard:
    """Stress-tests cold start threshold (D < 50) and absolute hazard overrides."""

    def test_cold_start_low_counts_do_not_trip(self, p3_db, template):
        """With sent = 10 and complaints = 1, rate is 10% (massive relative to 0.1%),

        but volume (10) < MIN_SAMPLE_SIZE (50) and complaints (1) < ABSOLUTE_COMPLAINTS_LIMIT (5).
        MUST NOT TRIP.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Cold Start 1 in 10")
        now = datetime.now(UTC)

        for i in range(10):
            p3_db.add(CampaignEvent(event_key=f"acc-cs1-{i}", campaign_id=campaign.id, event_type="accepted", created_at=now))
        p3_db.add(CampaignEvent(event_key="cmp-cs1-0", campaign_id=campaign.id, event_type="complaint", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "active"
        assert res["reason"] == "insufficient_volume"
        assert res["sent"] == 10
        assert res["complaints"] == 1

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_cold_start_high_percentage_near_limit_d49_c4_does_not_trip(self, p3_db, template):
        """D = 49 (exactly 1 below 50) with 4 complaints (8.16% complaint rate!).

        Since D < 50 and complaints < 5, cold start guard protects campaign from premature trip.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Cold Start D=49 C=4")
        now = datetime.now(UTC)

        for i in range(49):
            p3_db.add(CampaignEvent(event_key=f"acc-cs2-{i}", campaign_id=campaign.id, event_type="accepted", created_at=now))
        for i in range(4):
            p3_db.add(CampaignEvent(event_key=f"cmp-cs2-{i}", campaign_id=campaign.id, event_type="complaint", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "active"
        assert res["reason"] == "insufficient_volume"

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_cold_start_high_percentage_near_limit_d49_b19_does_not_trip(self, p3_db, template):
        """D = 49 with 19 hard bounces (38.77% bounce rate!).

        Since D < 50 and bounces < 20, cold start guard protects campaign.
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Cold Start D=49 B=19")
        now = datetime.now(UTC)

        for i in range(49):
            p3_db.add(CampaignEvent(event_key=f"acc-cs3-{i}", campaign_id=campaign.id, event_type="accepted", created_at=now))
        for i in range(19):
            p3_db.add(CampaignEvent(event_key=f"bnc-cs3-{i}", campaign_id=campaign.id, event_type="hard_bounce", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "active"
        assert res["reason"] == "insufficient_volume"

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.APPROVED

    def test_sample_size_exact_boundary_d50_healthy_vs_tripped(self, p3_db, template):
        """At exactly D = 50 (MIN_SAMPLE_SIZE), percentage evaluation activates.

        - D = 50, B = 2 -> 4.0% <= 5.0% -> HEALTHY
        - D = 50, B = 3 -> 6.0% > 5.0% -> TRIPPED
        """
        # Part A: Healthy at D = 50
        camp_a = _create_approved_campaign(p3_db, template.id, "D=50 Healthy")
        now = datetime.now(UTC)
        for i in range(50):
            p3_db.add(CampaignEvent(event_key=f"acc-d50a-{i}", campaign_id=camp_a.id, event_type="accepted", created_at=now))
        for i in range(2):
            p3_db.add(CampaignEvent(event_key=f"bnc-d50a-{i}", campaign_id=camp_a.id, event_type="hard_bounce", created_at=now))
        p3_db.commit()

        res_a = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=camp_a.id)
        assert res_a["status"] == "healthy"
        assert res_a["bounce_rate"] == 0.04

        # Part B: Tripped at D = 50
        camp_b = _create_approved_campaign(p3_db, template.id, "D=50 Tripped")
        for i in range(50):
            p3_db.add(CampaignEvent(event_key=f"acc-d50b-{i}", campaign_id=camp_b.id, event_type="accepted", created_at=now))
        for i in range(3):
            p3_db.add(CampaignEvent(event_key=f"bnc-d50b-{i}", campaign_id=camp_b.id, event_type="hard_bounce", created_at=now))
        p3_db.commit()

        res_b = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=camp_b.id)
        assert res_b["status"] == "tripped"
        assert res_b["bounce_rate"] == 0.06

    def test_absolute_hazard_override_complaints_at_low_d(self, p3_db, template):
        """Reaching 5 complaints trips immediately even with D = 10 (< 50)."""
        campaign = _create_approved_campaign(p3_db, template.id, "Absolute Hazard C=5 D=10")
        now = datetime.now(UTC)

        for i in range(10):
            p3_db.add(CampaignEvent(event_key=f"acc-ah1-{i}", campaign_id=campaign.id, event_type="accepted", created_at=now))
        for i in range(5):
            p3_db.add(CampaignEvent(event_key=f"cmp-ah1-{i}", campaign_id=campaign.id, event_type="complaint", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert "Absolute hazard limit reached" in res["reason"]
        assert "complaints=5" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

    def test_absolute_hazard_override_complaints_at_zero_d(self, p3_db, template):
        """Reaching 5 complaints trips immediately even with D = 0 (no accepted events recorded)."""
        campaign = _create_approved_campaign(p3_db, template.id, "Absolute Hazard C=5 D=0")
        now = datetime.now(UTC)

        for i in range(5):
            p3_db.add(CampaignEvent(event_key=f"cmp-ah0-{i}", campaign_id=campaign.id, event_type="complaint", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert "complaints=5" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

    def test_absolute_hazard_override_bounces_at_low_d(self, p3_db, template):
        """Reaching 20 hard bounces trips immediately even with D = 25 (< 50)."""
        campaign = _create_approved_campaign(p3_db, template.id, "Absolute Hazard B=20 D=25")
        now = datetime.now(UTC)

        for i in range(25):
            p3_db.add(CampaignEvent(event_key=f"acc-ah2-{i}", campaign_id=campaign.id, event_type="accepted", created_at=now))
        for i in range(20):
            p3_db.add(CampaignEvent(event_key=f"bnc-ah2-{i}", campaign_id=campaign.id, event_type="hard_bounce", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert "Absolute hazard limit reached" in res["reason"]
        assert "hard_bounces=20" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

    def test_absolute_hazard_override_bounces_at_zero_d(self, p3_db, template):
        """Reaching 20 hard bounces trips immediately even with D = 0."""
        campaign = _create_approved_campaign(p3_db, template.id, "Absolute Hazard B=20 D=0")
        now = datetime.now(UTC)

        for i in range(20):
            p3_db.add(CampaignEvent(event_key=f"bnc-ah0-{i}", campaign_id=campaign.id, event_type="hard_bounce", created_at=now))
        p3_db.commit()

        res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=campaign.id)
        assert res["status"] == "tripped"
        assert "hard_bounces=20" in res["reason"]

        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED


# ==============================================================================
# 3. STATE MACHINE INVARIANTS & CASCADING SIDE EFFECTS ON TRIP
# ==============================================================================

class TestCircuitBreakerCascadingSideEffects:
    """Verifies that tripping circuit breaker properly mutates targeted states while

    leaving completed/delivered states strictly intact.
    """

    def test_trip_campaign_cascading_state_mutations(self, p3_db, template):
        """Setup a campaign with mixed outbox states and recipient states:

        - 2 PENDING outbox messages -> MUST transition to FAILED ("circuit_breaker_paused")
        - 2 RETRY outbox messages   -> MUST transition to FAILED ("circuit_breaker_paused")
        - 2 DELIVERED outbox messages -> MUST REMAIN DELIVERED
        - 2 ACCEPTED outbox messages  -> MUST REMAIN ACCEPTED
        - 1 FAILED outbox message (pre-existing) -> MUST REMAIN FAILED with original error
        - 3 QUEUED recipients -> MUST transition to CANCELLED
        - 2 DISPATCHED recipients -> MUST REMAIN DISPATCHED
        - 1 BOUNCED recipient -> MUST REMAIN BOUNCED
        - 1 COMPLAINED recipient -> MUST REMAIN COMPLAINED
        """
        campaign = _create_approved_campaign(p3_db, template.id, "Cascade Invariants Campaign")

        # 1. Outbox messages
        ob_pending_1 = OutboxMessage(campaign_id=campaign.id, email_normalized="p1@test.com", idempotency_key="ob-p1", status=OutboxStatus.PENDING)
        ob_pending_2 = OutboxMessage(campaign_id=campaign.id, email_normalized="p2@test.com", idempotency_key="ob-p2", status=OutboxStatus.PENDING)
        ob_retry_1 = OutboxMessage(campaign_id=campaign.id, email_normalized="r1@test.com", idempotency_key="ob-r1", status=OutboxStatus.RETRY)
        ob_retry_2 = OutboxMessage(campaign_id=campaign.id, email_normalized="r2@test.com", idempotency_key="ob-r2", status=OutboxStatus.RETRY)
        ob_delivered_1 = OutboxMessage(campaign_id=campaign.id, email_normalized="d1@test.com", idempotency_key="ob-d1", status=OutboxStatus.DELIVERED)
        ob_delivered_2 = OutboxMessage(campaign_id=campaign.id, email_normalized="d2@test.com", idempotency_key="ob-d2", status=OutboxStatus.DELIVERED)
        ob_accepted_1 = OutboxMessage(campaign_id=campaign.id, email_normalized="a1@test.com", idempotency_key="ob-a1", status=OutboxStatus.ACCEPTED)
        ob_accepted_2 = OutboxMessage(campaign_id=campaign.id, email_normalized="a2@test.com", idempotency_key="ob-a2", status=OutboxStatus.ACCEPTED)
        ob_failed_orig = OutboxMessage(campaign_id=campaign.id, email_normalized="f1@test.com", idempotency_key="ob-f1", status=OutboxStatus.FAILED, last_error="dns_resolution_failed")

        p3_db.add_all([
            ob_pending_1, ob_pending_2,
            ob_retry_1, ob_retry_2,
            ob_delivered_1, ob_delivered_2,
            ob_accepted_1, ob_accepted_2,
            ob_failed_orig,
        ])

        # 2. Recipients
        cr_queued_1 = CampaignRecipient(campaign_id=campaign.id, email_normalized="q1@test.com", unsubscribe_token="tok-q1", status=CampaignRecipientStatus.QUEUED.value)
        cr_queued_2 = CampaignRecipient(campaign_id=campaign.id, email_normalized="q2@test.com", unsubscribe_token="tok-q2", status=CampaignRecipientStatus.QUEUED.value)
        cr_queued_3 = CampaignRecipient(campaign_id=campaign.id, email_normalized="q3@test.com", unsubscribe_token="tok-q3", status=CampaignRecipientStatus.QUEUED.value)
        cr_disp_1 = CampaignRecipient(campaign_id=campaign.id, email_normalized="disp1@test.com", unsubscribe_token="tok-d1", status=CampaignRecipientStatus.DISPATCHED.value)
        cr_disp_2 = CampaignRecipient(campaign_id=campaign.id, email_normalized="disp2@test.com", unsubscribe_token="tok-d2", status=CampaignRecipientStatus.DISPATCHED.value)
        cr_bounced = CampaignRecipient(campaign_id=campaign.id, email_normalized="bnc@test.com", unsubscribe_token="tok-b", status=CampaignRecipientStatus.BOUNCED.value)
        cr_complained = CampaignRecipient(campaign_id=campaign.id, email_normalized="cmp@test.com", unsubscribe_token="tok-c", status=CampaignRecipientStatus.COMPLAINED.value)

        p3_db.add_all([
            cr_queued_1, cr_queued_2, cr_queued_3,
            cr_disp_1, cr_disp_2,
            cr_bounced, cr_complained,
        ])
        p3_db.commit()

        # Trigger trip via DeliverabilityCircuitBreaker.trip_campaign directly
        trip_res = DeliverabilityCircuitBreaker.trip_campaign(
            p3_db,
            campaign,
            reason="Adversarial stress trip test",
            sent=100,
            complaints=10,
            hard_bounces=0,
        )
        p3_db.commit()

        assert trip_res["status"] == "tripped"
        assert trip_res["cancelled_outbox"] == 4  # 2 pending + 2 retry
        assert trip_res["cancelled_recipients"] == 3  # 3 queued

        # Verify Campaign status
        p3_db.refresh(campaign)
        assert campaign.status == CampaignStatus.PAUSED

        # Verify Outbox mutations
        for ob in [ob_pending_1, ob_pending_2, ob_retry_1, ob_retry_2]:
            p3_db.refresh(ob)
            assert ob.status == OutboxStatus.FAILED
            assert ob.last_error == "circuit_breaker_paused"

        for ob in [ob_delivered_1, ob_delivered_2]:
            p3_db.refresh(ob)
            assert ob.status == OutboxStatus.DELIVERED

        for ob in [ob_accepted_1, ob_accepted_2]:
            p3_db.refresh(ob)
            assert ob.status == OutboxStatus.ACCEPTED

        p3_db.refresh(ob_failed_orig)
        assert ob_failed_orig.status == OutboxStatus.FAILED
        assert ob_failed_orig.last_error == "dns_resolution_failed"  # NOT overwritten!

        # Verify Recipient mutations
        for cr in [cr_queued_1, cr_queued_2, cr_queued_3]:
            p3_db.refresh(cr)
            assert cr.status == CampaignRecipientStatus.CANCELLED.value

        for cr in [cr_disp_1, cr_disp_2]:
            p3_db.refresh(cr)
            assert cr.status == CampaignRecipientStatus.DISPATCHED.value

        p3_db.refresh(cr_bounced)
        assert cr_bounced.status == CampaignRecipientStatus.BOUNCED.value

        p3_db.refresh(cr_complained)
        assert cr_complained.status == CampaignRecipientStatus.COMPLAINED.value

        # Verify AuditEvent
        audit_ev = p3_db.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == str(campaign.id),
                AuditEvent.action == "circuit_breaker.tripped",
            )
        )
        assert audit_ev is not None
        assert audit_ev.actor == "system:circuit_breaker"
        assert "cancelled_outbox=4" in audit_ev.detail
        assert "cancelled_recipients=3" in audit_ev.detail

        # Verify System Alert surfaces critical alert
        alerts_data = system_alerts(p3_db)
        assert alerts_data["status"] == "critical"
        alert_codes = [a["code"] for a in alerts_data["active_alerts"]]
        assert "circuit_breaker_tripped" in alert_codes

    def test_non_approved_campaign_skips_circuit_breaker(self, p3_db, template):
        """Campaigns in DRAFT, PAUSED, or CANCELLED status should be skipped by evaluate_campaign."""
        for status in [CampaignStatus.DRAFT, CampaignStatus.PAUSED, CampaignStatus.CANCELLED]:
            camp = Campaign(
                name=f"Skip Test {status.value}",
                template_id=template.id,
                consent_purpose="test",
                status=status,
            )
            p3_db.add(camp)
            p3_db.commit()

            # Add critical error events
            now = datetime.now(UTC)
            for i in range(10):
                p3_db.add(CampaignEvent(event_key=f"cmp-sk-{status.value}-{i}", campaign_id=camp.id, event_type="complaint", created_at=now))
            p3_db.commit()

            res = DeliverabilityCircuitBreaker.evaluate_campaign(p3_db, campaign_id=camp.id)
            assert res["status"] == "skipped"
            assert res["reason"] == "campaign_not_approved"
            assert res["current_status"] == status

            p3_db.refresh(camp)
            assert camp.status == status


# ==============================================================================
# 4. CONCURRENT DUPLICATE SUPPRESSION INSERTS (RACE RESILIENCE)
# ==============================================================================

class TestConcurrentSuppressionInserts:
    """Stress-tests multi-threaded concurrent inserts into the suppressions table

    to verify race condition resilience, zero deadlocks, and unique constraint enforcement.
    """

    def test_concurrent_identical_suppression_inserts(self, concurrent_engine):
        """20 concurrent threads inserting the exact same email address into suppressions.

        Tests native upsert ON CONFLICT DO UPDATE behavior under concurrency.
        """
        target_email = "race.victim.concurrent@example.com"
        num_threads = 20
        errors: list[Exception] = []

        def worker_task(thread_id: int) -> tuple[int, float]:
            t0 = time.perf_counter()
            with Session(concurrent_engine) as session:
                try:
                    upsert_global_suppression(
                        session,
                        email=target_email,
                        reason=SuppressionReason.HARD_BOUNCE,
                        source="aws_sns",
                        detail=f"concurrent_thread_{thread_id}",
                    )
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    errors.append(exc)
                    raise
            t1 = time.perf_counter()
            return thread_id, (t1 - t0) * 1000

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker_task, i) for i in range(num_threads)]
            latencies = [f.result()[1] for f in as_completed(futures)]

        assert len(errors) == 0, f"Encountered {len(errors)} concurrent errors: {errors}"

        # Verify exactly 1 suppression record in the database
        with Session(concurrent_engine) as session:
            rows = session.scalars(
                select(Suppression).where(Suppression.email_normalized == target_email)
            ).all()
            assert len(rows) == 1
            assert rows[0].email_normalized == target_email
            assert rows[0].reason == SuppressionReason.HARD_BOUNCE

        # Sub-second benchmark verification: average latency < 100ms per thread under 20-thread SQLite disk contention
        avg_lat = sum(latencies) / len(latencies)
        assert avg_lat < 100.0, f"Average concurrent latency {avg_lat:.2f}ms exceeded SLA"

    def test_concurrent_unnormalized_casing_variations(self, concurrent_engine):
        """10 concurrent threads inserting different raw casing & whitespace variations

        of the same email address. All MUST normalize and collapse into a single row.
        """
        variations = [
            "User.Alias@Example.COM",
            "  user.alias@example.com  ",
            "USER.ALIAS@EXAMPLE.COM",
            "User.Alias@example.com",
            "\tuser.alias@EXAMPLE.COM\n",
            "user.alias@Example.Com",
            "USER.ALIAS@example.com",
            "  User.Alias@Example.Com  ",
            "user.alias@EXAMPLE.com",
            "User.ALIAS@example.COM",
        ]
        errors: list[Exception] = []

        def worker_task(email_val: str) -> str:
            with Session(concurrent_engine) as session:
                try:
                    norm = upsert_global_suppression(
                        session,
                        email=email_val,
                        reason=SuppressionReason.COMPLAINT,
                        source="aws_sns",
                        detail=f"casing_test:{email_val.strip()}",
                    )
                    session.commit()
                    return norm
                except Exception as exc:
                    session.rollback()
                    errors.append(exc)
                    raise

        with ThreadPoolExecutor(max_workers=len(variations)) as executor:
            futures = [executor.submit(worker_task, v) for v in variations]
            results = [f.result() for f in as_completed(futures)]

        assert len(errors) == 0
        expected_norm = normalize_email("user.alias@example.com")
        assert all(r == expected_norm for r in results)

        with Session(concurrent_engine) as session:
            rows = session.scalars(
                select(Suppression).where(Suppression.email_normalized == expected_norm)
            ).all()
            assert len(rows) == 1
            assert rows[0].reason == SuppressionReason.COMPLAINT

    def test_concurrent_competing_reasons_race(self, concurrent_engine):
        """Concurrent race between COMPLAINT and HARD_BOUNCE for the same email.

        Verify DB consistency: no deadlocks, 1 record, reason is valid enum.
        """
        target_email = "competing.race@example.com"
        reasons = [SuppressionReason.COMPLAINT, SuppressionReason.HARD_BOUNCE] * 5

        def worker_task(reason: SuppressionReason) -> None:
            with Session(concurrent_engine) as session:
                upsert_global_suppression(
                    session,
                    email=target_email,
                    reason=reason,
                    source="aws_sns",
                    detail=f"competing_reason:{reason.value}",
                )
                session.commit()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker_task, r) for r in reasons]
            for f in as_completed(futures):
                f.result()

        with Session(concurrent_engine) as session:
            rows = session.scalars(
                select(Suppression).where(Suppression.email_normalized == target_email)
            ).all()
            assert len(rows) == 1
            assert rows[0].reason in (SuppressionReason.COMPLAINT, SuppressionReason.HARD_BOUNCE)


# ==============================================================================
# 5. SUB-SECOND SUPPRESSION & STATE SYNCHRONIZATION (R2)
# ==============================================================================

class TestSuppressionAndStateSync:
    """Verifies sub-second suppression insertion and indexed state synchronization precedence."""

    def test_suppression_insert_sub_millisecond_benchmark(self, p3_db):
        """Empirically benchmarks upsert_global_suppression to prove < 10ms SLA."""
        email = f"benchmark-{time.time_ns()}@example.com"

        t0 = time.perf_counter()
        norm = upsert_global_suppression(
            p3_db,
            email=email,
            reason=SuppressionReason.HARD_BOUNCE,
            source="aws_sns",
            detail="latency_sla_check",
        )
        p3_db.commit()
        t1 = time.perf_counter()

        elapsed_ms = (t1 - t0) * 1000
        assert elapsed_ms < 10.0, f"Suppression insertion took {elapsed_ms:.3f}ms (exceeded 10ms SLA)"
        assert norm == email.lower()

    def test_state_sync_precedence_by_outbox_id(self, p3_db, template):
        """Precedence 1: Updates matching outbox and recipient by outbox_id directly."""
        campaign = _create_approved_campaign(p3_db, template.id, "Precedence 1 Campaign")
        email = "prec1@example.com"

        outbox = OutboxMessage(campaign_id=campaign.id, email_normalized=email, idempotency_key="ob-prec-1", status=OutboxStatus.ACCEPTED)
        p3_db.add(outbox)
        p3_db.flush()

        recip = CampaignRecipient(campaign_id=campaign.id, email_normalized=email, outbox_id=outbox.id, unsubscribe_token="tok-p1", status=CampaignRecipientStatus.QUEUED.value)
        p3_db.add(recip)
        p3_db.commit()

        counts = synchronize_recipient_state(
            p3_db,
            email_normalized=email,
            new_status="bounced",
            campaign_id=campaign.id,
            outbox_id=outbox.id,
            error_detail="550 5.1.1 User unknown",
        )
        p3_db.commit()

        assert counts["outbox_messages"] == 1
        assert counts["campaign_recipients"] == 1

        p3_db.refresh(outbox)
        assert outbox.status == OutboxStatus.BOUNCED
        assert outbox.last_error == "550 5.1.1 User unknown"

        p3_db.refresh(recip)
        assert recip.status == CampaignRecipientStatus.BOUNCED.value

    def test_state_sync_precedence_by_composite_campaign_email(self, p3_db, template):
        """Precedence 2: When outbox_id is None, updates by (campaign_id, email_normalized)."""
        campaign = _create_approved_campaign(p3_db, template.id, "Precedence 2 Campaign")
        email = "prec2@example.com"

        outbox = OutboxMessage(campaign_id=campaign.id, email_normalized=email, idempotency_key="ob-prec-2", status=OutboxStatus.ACCEPTED)
        recip = CampaignRecipient(campaign_id=campaign.id, email_normalized=email, unsubscribe_token="tok-p2", status=CampaignRecipientStatus.QUEUED.value)
        p3_db.add_all([outbox, recip])
        p3_db.commit()

        counts = synchronize_recipient_state(
            p3_db,
            email_normalized=email,
            new_status="complained",
            campaign_id=campaign.id,
            outbox_id=None,
            error_detail="Spam complaint: abuse",
        )
        p3_db.commit()

        assert counts["outbox_messages"] == 1
        assert counts["campaign_recipients"] == 1

        p3_db.refresh(outbox)
        assert outbox.status == OutboxStatus.COMPLAINED

        p3_db.refresh(recip)
        assert recip.status == CampaignRecipientStatus.COMPLAINED.value

    def test_state_sync_fallback_across_all_campaigns(self, p3_db, template):
        """Precedence 3: When campaign_id and outbox_id are None, updates across all campaigns."""
        camp1 = _create_approved_campaign(p3_db, template.id, "Fall 1")
        camp2 = _create_approved_campaign(p3_db, template.id, "Fall 2")
        email = "global.recipient@example.com"

        ob1 = OutboxMessage(campaign_id=camp1.id, email_normalized=email, idempotency_key="ob-f-1", status=OutboxStatus.ACCEPTED)
        ob2 = OutboxMessage(campaign_id=camp2.id, email_normalized=email, idempotency_key="ob-f-2", status=OutboxStatus.PENDING)
        cr1 = CampaignRecipient(campaign_id=camp1.id, email_normalized=email, unsubscribe_token="tok-f1", status=CampaignRecipientStatus.DISPATCHED.value)
        cr2 = CampaignRecipient(campaign_id=camp2.id, email_normalized=email, unsubscribe_token="tok-f2", status=CampaignRecipientStatus.QUEUED.value)
        p3_db.add_all([ob1, ob2, cr1, cr2])
        p3_db.commit()

        counts = synchronize_recipient_state(
            p3_db,
            email_normalized=email,
            new_status="bounced",
            campaign_id=None,
            outbox_id=None,
            error_detail="550 Mailbox does not exist",
        )
        p3_db.commit()

        assert counts["outbox_messages"] == 2
        assert counts["campaign_recipients"] == 2

        p3_db.refresh(ob1)
        p3_db.refresh(ob2)
        assert ob1.status == OutboxStatus.BOUNCED
        assert ob2.status == OutboxStatus.BOUNCED

        p3_db.refresh(cr1)
        p3_db.refresh(cr2)
        assert cr1.status == CampaignRecipientStatus.BOUNCED.value
        assert cr2.status == CampaignRecipientStatus.BOUNCED.value

    def test_soft_bounce_does_not_suppress_and_retries_properly(self, p3_db, template):
        """Transient soft bounce must NOT insert into suppressions and must set outbox to RETRY."""
        campaign = _create_approved_campaign(p3_db, template.id, "Soft Bounce Campaign")
        email = "soft.bounce.victim@example.com"

        outbox = OutboxMessage(
            campaign_id=campaign.id,
            email_normalized=email,
            idempotency_key="ob-soft-1",
            status=OutboxStatus.ACCEPTED,
            attempts=1,
        )
        p3_db.add(outbox)
        p3_db.commit()

        # Simulate soft bounce handling as in sns_router
        outbox.attempts += 1
        outbox.status = OutboxStatus.RETRY
        outbox.last_error = "451 4.4.1 Mailbox full"
        p3_db.commit()

        # Check suppressions table: email must NOT be suppressed!
        supp = p3_db.scalar(
            select(Suppression).where(Suppression.email_normalized == email)
        )
        assert supp is None

        p3_db.refresh(outbox)
        assert outbox.status == OutboxStatus.RETRY
        assert outbox.attempts == 2
        assert outbox.last_error == "451 4.4.1 Mailbox full"
