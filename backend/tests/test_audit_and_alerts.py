from datetime import UTC, datetime, timedelta

from app.auth import audit
from app.main import list_audit_logs, system_alerts
from app.models import CampaignEvent, OutboxMessage, OutboxStatus


def test_list_audit_logs(db):
    audit(db, "test.action_one", detail="first action")
    audit(db, "test.action_two", detail="second action")
    db.commit()

    logs = list_audit_logs(limit=10, db=db)
    assert len(logs) >= 2
    actions = [log_entry["action"] for log_entry in logs]
    assert "test.action_two" in actions
    assert "test.action_one" in actions


def test_system_alerts_healthy(db):
    res = system_alerts(db)
    assert res["status"] == "healthy"
    assert res["lagging_outbox_jobs"] == 0
    assert len(res["active_alerts"]) == 0


def test_system_alerts_queue_lag(db):
    now = datetime.now(UTC)
    overdue_time = now - timedelta(minutes=10)

    # Outbox message overdue by 10 minutes
    msg = OutboxMessage(
        campaign_id=1,
        email_normalized="lag@example.com",
        idempotency_key="lag-key-1",
        status=OutboxStatus.PENDING,
        next_attempt_at=overdue_time,
    )
    db.add(msg)
    db.commit()

    res = system_alerts(db)
    assert res["status"] in {"warning", "critical"}
    assert res["lagging_outbox_jobs"] == 1
    codes = [a["code"] for a in res["active_alerts"]]
    assert "queue_lag" in codes


def test_system_alerts_high_bounce_rate(db):
    now = datetime.now(UTC)

    # 10 dispatched events
    for i in range(10):
        db.add(
            CampaignEvent(
                event_key=f"accepted-alert-{i}",
                campaign_id=1,
                event_type="accepted",
                created_at=now,
            )
        )
    # 2 bounce events -> 2/10 = 20% bounce rate (> 5%)
    for i in range(2):
        db.add(
            CampaignEvent(
                event_key=f"bounce-alert-{i}",
                campaign_id=1,
                event_type="bounce",
                created_at=now,
            )
        )
    db.commit()

    res = system_alerts(db)
    assert res["status"] == "critical"
    codes = [a["code"] for a in res["active_alerts"]]
    assert "high_bounce_rate" in codes
