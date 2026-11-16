import pytest
from fastapi import HTTPException

from app.imports import ColumnMapping, validate_mapping
from app.main import enforce_rate_limit, safe_csv_cell
from app.models import normalize_email
from app.suppression import make_unsubscribe_token, verify_unsubscribe_token
from app.templates import TemplateInput, render_preview, template_values


def test_normalize_email():
    assert normalize_email("  Test.User@Example.COM  ") == "test.user@example.com"
    assert normalize_email("alice+tag@domain.co.uk") == "alice+tag@domain.co.uk"

    with pytest.raises(ValueError, match="Invalid email address"):
        normalize_email("not-an-email")

    with pytest.raises(ValueError, match="Invalid email address"):
        normalize_email("@example.com")


def test_safe_csv_cell_formula_injection():
    # Dangerous leading characters
    assert safe_csv_cell("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert safe_csv_cell("+1+2") == "'+1+2"
    assert safe_csv_cell("-5+2") == "'-5+2"
    assert safe_csv_cell("@SUM(1,2)") == "'@SUM(1,2)"
    assert safe_csv_cell("\t dangerous") == "'\t dangerous"
    assert safe_csv_cell("   =formula") == "'   =formula"

    # Safe characters
    assert safe_csv_cell("Regular text") == "Regular text"
    assert safe_csv_cell("user@example.com") == "user@example.com"
    assert safe_csv_cell(None) == ""
    assert safe_csv_cell(123) == "123"


def test_unsubscribe_token_lifecycle():
    token = make_unsubscribe_token(campaign_id=42, contact_id=99, email_normalized="user@example.com")
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[0] == "42"
    assert parts[1] == "99"
    assert len(parts[2]) == 64

    # Verify matching credentials
    assert verify_unsubscribe_token(token, 42, 99, "user@example.com") is True

    # Tampered email
    assert verify_unsubscribe_token(token, 42, 99, "attacker@example.com") is False

    # Tampered campaign ID
    assert verify_unsubscribe_token(token, 999, 99, "user@example.com") is False

    # Tampered contact ID
    assert verify_unsubscribe_token(token, 42, 100, "user@example.com") is False

    # Malformed token
    assert verify_unsubscribe_token("invalid-token", 42, 99, "user@example.com") is False


def test_template_html_sanitization_and_unsubscribe_enforcement():
    # Missing unsubscribe placeholder in HTML
    bad_template = TemplateInput(
        name="Bad",
        subject="No optout",
        sender_name="Sender",
        sender_email="sender@example.com",
        mailing_address="123 Road",
        html_body="<p>Hello world without placeholder</p>",
        text_body="Unsubscribe: {{unsubscribe_url}}",
    )
    with pytest.raises(HTTPException) as exc_info:
        template_values(bad_template)
    assert exc_info.value.status_code == 422

    # Missing unsubscribe placeholder in text
    bad_template2 = TemplateInput(
        name="Bad2",
        subject="No optout",
        sender_name="Sender",
        sender_email="sender@example.com",
        mailing_address="123 Road",
        html_body="<p><a href='{{unsubscribe_url}}'>Unsubscribe</a></p>",
        text_body="Plain text without placeholder",
    )
    with pytest.raises(HTTPException) as exc_info2:
        template_values(bad_template2)
    assert exc_info2.value.status_code == 422

    # Sanitization strips harmful tags (<script>, onclick, etc.)
    malicious_template = TemplateInput(
        name="Security Test",
        subject="Test",
        sender_name="Sender",
        sender_email="sender@example.com",
        mailing_address="123 Road",
        html_body="<script>alert(1)</script><img src=x onerror=alert(1)><p><a href='{{unsubscribe_url}}'>Unsubscribe</a></p>",
        text_body="Unsubscribe: {{unsubscribe_url}}",
    )
    values = template_values(malicious_template)
    assert "<script>" not in values["html_body"]
    assert "onerror" not in values["html_body"]
    assert "{{unsubscribe_url}}" in values["html_body"]

    # Preview rendering replaces placeholder with dummy preview link
    preview = render_preview(malicious_template)
    assert "https://example.test/unsubscribe/preview-token" in preview["html_body"]
    assert "https://example.test/unsubscribe/preview-token" in preview["text_body"]


def test_rate_limiter():
    key = "test_limiter_unique_key"
    # Should allow up to 3 calls
    for _ in range(3):
        enforce_rate_limit(key, limit=3, window=60)

    # 4th call exceeds limit
    with pytest.raises(HTTPException) as exc_info:
        enforce_rate_limit(key, limit=3, window=60)
    assert exc_info.value.status_code == 429


def test_column_mapping_validation():
    mapping = ColumnMapping(
        email="Email_Address",
        consent_source="Source",
        consented_at="Date",
        consent_purpose="Purpose",
        consent_provenance="Evidence",
        display_name="Full_Name",
    )
    headers = ["Email_Address", "Source", "Date", "Purpose", "Evidence", "Full_Name", "Extra"]
    validated = validate_mapping(mapping, headers)
    assert validated["email"] == "Email_Address"
    assert validated["display_name"] == "Full_Name"

    # Missing required header
    with pytest.raises(HTTPException) as exc_info:
        validate_mapping(mapping, ["Wrong_Header", "Source", "Date", "Purpose", "Evidence"])
    assert exc_info.value.status_code == 422


def test_frontend_dist_static_mount():
    from app.main import app, frontend_dist

    if frontend_dist.exists():
        assert any(getattr(r, "name", None) == "static" for r in app.routes)


def test_file_based_secret_loading(tmp_path):
    from app.settings import Settings

    secret_file = tmp_path / "secret_key.txt"
    secret_file.write_text("a-very-long-and-secure-file-secret-key-12345", encoding="utf-8")

    loaded_settings = Settings(
        secret_key_file=str(secret_file),
        app_env="test",
    )
    assert loaded_settings.secret_key == "a-very-long-and-secure-file-secret-key-12345"


def test_staging_e2e_rehearsal_drill():
    from scripts.staging_e2e_drill import run_staging_drill

    results = run_staging_drill()
    assert results["database"] == "initialized"
    assert results["mfa_auth"] == "verified"
    assert results["contacts_committed"] == 3
    assert results["template"] == "sanitized_and_persisted"
    assert results["queued_jobs"] == 3
    assert results["dispatched_jobs"] == 3
    assert results["webhook_bounce_processed"] is True
    assert results["unsubscribe_verified"] is True
    assert results["reporting_and_metrics"] == "verified"
    assert results["audit_events_count"] > 5


def test_security_audit_script():
    from scripts.security_audit import run_security_audit

    exit_code = run_security_audit()
    assert exit_code == 0


def test_domain_dns_verification_tool():
    from scripts.verify_domain_dns import analyze_dmarc, analyze_spf, run_domain_check

    # 1. Run full domain check
    exit_code = run_domain_check("example.org")
    assert exit_code == 0

    # 2. SPF analyzer tests
    safe_spf = analyze_spf("v=spf1 include:_spf.example.org ~all")
    assert safe_spf["valid"] is True
    assert safe_spf["all_mechanism"] == "~all"

    dangerous_spf = analyze_spf("v=spf1 +all")
    assert dangerous_spf["valid"] is False
    assert any("CRITICAL" in err for err in dangerous_spf["issues"])

    # 3. DMARC analyzer tests
    strict_dmarc = analyze_dmarc("v=DMARC1; p=reject; rua=mailto:dmarc@example.org")
    assert strict_dmarc["valid"] is True
    assert strict_dmarc["policy"] == "reject"

    invalid_dmarc = analyze_dmarc("v=DMARC1; p=unsupported")
    assert invalid_dmarc["valid"] is False


def test_pilot_audience_audit_tool(db):
    from datetime import UTC, datetime

    from app.models import (
        Campaign,
        CampaignStatus,
        ConsentRecord,
        Contact,
        EmailTemplate,
        Suppression,
        SuppressionReason,
    )
    from scripts.audit_pilot_audience import audit_campaign_pilot

    # 1. Setup clean contact with consent
    contact = Contact(email="pilot.user@example.org", email_normalized="pilot.user@example.org")
    db.add(contact)
    db.flush()

    consent = ConsentRecord(
        contact_id=contact.id,
        source="pilot_signup",
        consented_at=datetime.now(UTC),
        purpose="pilot_launch",
        provenance="form_id=pilot_42",
    )
    db.add(consent)

    tpl = EmailTemplate(
        name="Pilot Tpl",
        subject="Pilot Subject",
        sender_name="Pilot Sender",
        sender_email="pilot@example.org",
        mailing_address="123 Pilot St",
        html_body="<p>Hello <a href='{{unsubscribe_url}}'>Unsub</a></p>",
        text_body="Hello {{unsubscribe_url}}",
    )
    db.add(tpl)
    db.flush()

    campaign = Campaign(
        name="Pilot Audience Campaign",
        template_id=tpl.id,
        consent_purpose="pilot_launch",
        status=CampaignStatus.APPROVED,
        recipient_snapshot=[
            {"contact_id": contact.id, "email": "pilot.user@example.org", "display_name": "Pilot User"}
        ],
    )
    db.add(campaign)
    db.commit()

    # Should pass audit
    audit_res = audit_campaign_pilot(campaign.id, db=db)
    assert audit_res["passed"] is True
    assert audit_res["recipient_count"] == 1

    # Add recipient to suppression -> audit should fail
    db.add(
        Suppression(
            email_normalized="pilot.user@example.org",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="test",
        )
    )
    db.commit()

    audit_res_suppressed = audit_campaign_pilot(campaign.id, db=db)
    assert audit_res_suppressed["passed"] is False
    assert any("GLOBAL SUPPRESSION" in err for err in audit_res_suppressed["issues"])


def test_api_prefix_path_resolution():
    public_paths = {"/health", "/ready", "/auth/login", "/webhooks/provider"}

    test_paths = [
        ("/api/auth/login", "/auth/login", True),
        ("/api/ready", "/ready", True),
        ("/api/health", "/health", True),
        ("/api/contacts", "/contacts", False),
        ("/api/campaigns", "/campaigns", False),
        ("/health", "/health", True),
        ("/ready", "/ready", True),
    ]

    for raw, expected_stripped, expected_public in test_paths:
        path = raw[4:] if raw.startswith("/api/") else raw
        assert path == expected_stripped
        assert (path in public_paths) is expected_public



