"""Pilot Audience Consent & Permission Verification Tool.

Audits a proposed campaign recipient snapshot or staged audience against
stringent permission-based criteria before initial dispatch:
  1. Audience size constraint (enforces pilot cap)
  2. Affirmative consent evidence provenance
  3. Purpose alignment check
  4. Global suppression cross-referencing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Campaign, ConsentRecord, Suppression
from app.settings import settings


def audit_campaign_pilot(campaign_id: int, db=None) -> dict[str, Any]:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return {"status": "error", "message": f"Campaign {campaign_id} not found"}

        recipients = campaign.recipient_snapshot or []
        print(f"=== Auditing Pilot Audience for Campaign #{campaign.id}: '{campaign.name}' ===")
        print(f"Consent Purpose : {campaign.consent_purpose}")
        print(f"Status          : {campaign.status.value}")
        print(f"Audience Size   : {len(recipients)} recipients")

        issues: list[str] = []
        warnings: list[str] = []

        # 1. Size cap check
        if len(recipients) == 0:
            issues.append("Audience is empty. No recipients to audit.")
        elif len(recipients) > settings.max_campaign_recipients:
            issues.append(
                f"Audience size ({len(recipients)}) exceeds MAX_CAMPAIGN_RECIPIENTS ({settings.max_campaign_recipients})."
            )

        # 2. Global suppression cross-reference
        suppressed_set = set(db.scalars(select(Suppression.email_normalized)).all())

        for idx, r in enumerate(recipients, 1):
            email = r.get("email")
            contact_id = r.get("contact_id")

            if not email:
                issues.append(f"Row {idx}: Missing email address in snapshot.")
                continue

            if email in suppressed_set:
                issues.append(f"Row {idx} ({email}): Recipient is in the GLOBAL SUPPRESSION list!")

            # Verify underlying consent record exists and purpose matches
            if contact_id:
                consent = db.scalar(
                    select(ConsentRecord).where(
                        ConsentRecord.contact_id == contact_id,
                        ConsentRecord.purpose == campaign.consent_purpose,
                    )
                )
                if consent is None:
                    issues.append(f"Row {idx} ({email}): No matching consent record for purpose '{campaign.consent_purpose}'.")
                elif not consent.provenance:
                    warnings.append(f"Row {idx} ({email}): Consent evidence provenance details are blank.")

        passed = len(issues) == 0
        print("\nAUDIT OUTCOME:")
        print(f"  Valid / Compliant : {passed}")
        print(f"  Critical Issues   : {len(issues)}")
        print(f"  Warnings          : {len(warnings)}")

        for w in warnings:
            print(f"    [WARN] {w}")
        for err in issues:
            print(f"    [FAIL] {err}")

        if passed:
            print("\n[PASS] Pilot audience verified compliant with permission-based standards.")
        else:
            print("\n[FAIL] Pilot audience failed verification. Do NOT dispatch.")

        return {
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "passed": passed,
            "recipient_count": len(recipients),
            "issues": issues,
            "warnings": warnings,
        }
    finally:
        if close_db:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit pilot audience for permission compliance")
    parser.add_argument("campaign_id", type=int, help="Campaign ID to audit")
    args = parser.parse_args()
    res = audit_campaign_pilot(args.campaign_id)
    sys.exit(0 if res.get("passed") else 1)


if __name__ == "__main__":
    main()
