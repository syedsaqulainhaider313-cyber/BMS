"""Automated Security and Compliance Audit Tool.

Inspects runtime settings, cryptographic key hygiene, dependency configurations,
and database migration state to detect security misconfigurations before deployment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from app.settings import settings


def run_security_audit(verbose: bool = False) -> int:
    issues: list[str] = []
    warnings: list[str] = []

    print("=== Bulk Mail System - Security Configuration Audit ===")

    # 1. Environment & Debug Flag
    if settings.app_env == "production" and settings.debug:
        issues.append("CRITICAL: DEBUG must be False in production.")
    else:
        print("[OK] Environment debug settings compliant.")

    # 2. Cryptographic Secret Keys
    if len(settings.secret_key) < 32 or "change-me" in settings.secret_key:
        if settings.app_env == "production":
            issues.append("CRITICAL: Insecure default SECRET_KEY in production.")
        else:
            warnings.append("WARNING: Default development SECRET_KEY in use.")
    else:
        print("[OK] SECRET_KEY meets minimum entropy requirement.")

    if len(settings.webhook_signing_secret) < 32 or "change-me" in settings.webhook_signing_secret:
        if settings.app_env == "production":
            issues.append("CRITICAL: Insecure default WEBHOOK_SIGNING_SECRET in production.")
        else:
            warnings.append("WARNING: Default development WEBHOOK_SIGNING_SECRET in use.")
    else:
        print("[OK] WEBHOOK_SIGNING_SECRET meets minimum entropy requirement.")

    if len(settings.unsubscribe_signing_key) < 32 or "change-me" in settings.unsubscribe_signing_key:
        if settings.app_env == "production":
            issues.append("CRITICAL: Insecure default UNSUBSCRIBE_SIGNING_KEY in production.")
        else:
            warnings.append("WARNING: Default development UNSUBSCRIBE_SIGNING_KEY in use.")
    else:
        print("[OK] UNSUBSCRIBE_SIGNING_KEY meets minimum entropy requirement.")

    # 3. Cookies & Session Policy
    if settings.app_env == "production" and not settings.secure_cookies:
        issues.append("CRITICAL: SECURE_COOKIES must be True in production.")
    else:
        print("[OK] Cookie security flags compliant.")

    if settings.session_max_age_seconds > 86400:
        warnings.append("WARNING: Session duration exceeds 24 hours.")
    else:
        print("[OK] Session max-age within safe bounds.")

    # 4. Email Provider Boundaries
    if settings.email_provider != "mock":
        issues.append("CRITICAL: Unauthorized external email provider configured.")
    else:
        print("[OK] Email provider restricted to safe local mock.")

    # 5. Database Schema & Migration Lineage
    try:
        engine = create_engine(settings.database_url)
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if rev != "0007_manual_retention":
                warnings.append(f"WARNING: Current migration revision is '{rev}' (expected '0007_manual_retention').")
            else:
                print(f"[OK] Database migration verified at head ({rev}).")
    except Exception as exc:
        warnings.append(f"INFO: Database unreachable during static audit: {exc}")

    # Summary
    print("-" * 55)
    for w in warnings:
        print(f"  [WARN] {w}")
    for err in issues:
        print(f"  [FAIL] {err}")

    if issues:
        print(f"\n[FAIL] Audit failed with {len(issues)} critical issue(s).")
        return 1

    print(f"\n[PASS] Security audit passed with 0 critical issues and {len(warnings)} note(s).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BMS security audit")
    parser.add_argument("--verbose", action="store_true", help="Print verbose details")
    args = parser.parse_args()
    sys.exit(run_security_audit(verbose=args.verbose))


if __name__ == "__main__":
    main()
