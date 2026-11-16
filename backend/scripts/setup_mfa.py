"""Operator MFA setup drill and secret generator.

Generates a secure RFC 6238 Base32 TOTP secret, displays the standard
otpauth:// URI for authenticator applications (Google Authenticator,
1Password, Bitwarden, etc.), and verifies that the operator can generate
a valid 6-digit code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add backend to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import compute_totp_code, generate_totp_secret, get_totp_uri, verify_totp_code
from app.settings import settings


def run_setup(verify_code: str | None = None) -> int:
    secret = generate_totp_secret()
    uri = get_totp_uri(secret, settings.operator_username, issuer="BMS")
    current_code = compute_totp_code(secret)

    print("=== Bulk Mail System - Operator MFA Setup ===")
    print(f"Operator Username : {settings.operator_username}")
    print(f"Generated Secret  : {secret}")
    print(f"Authenticator URI : {uri}")
    print(f"Current Test Code : {current_code}")
    print("---------------------------------------------")

    if verify_code:
        valid = verify_totp_code(secret, verify_code)
        if valid:
            print(f"[OK] Verification succeeded for code: {verify_code}")
        else:
            print(f"[FAIL] Verification failed for code: {verify_code}")
            return 1
    else:
        # Self-test current code
        assert verify_totp_code(secret, current_code)
        print("[OK] Algorithm self-test verified.")

    print("\nTo enable MFA in production or staging, add this to your .env file:")
    print(f"OPERATOR_TOTP_SECRET={secret}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and test Operator TOTP MFA secret")
    parser.add_argument(
        "--verify",
        type=str,
        default=None,
        help="Optional code to verify against the generated secret",
    )
    args = parser.parse_args()
    sys.exit(run_setup(verify_code=args.verify))


if __name__ == "__main__":
    main()
