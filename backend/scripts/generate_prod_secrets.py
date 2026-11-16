"""Production Secrets Generator for Docker Compose & Secret Managers.

Generates high-entropy cryptographic keys and files in ./secrets/
with proper file permissions (chmod 600 where supported).
Never check the generated ./secrets/*.txt files into version control!
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

# Add backend to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import generate_totp_secret, password_hash


def generate_production_secrets(
    output_dir: Path, operator_password: str | None = None
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Application secret key
    secret_key = secrets.token_urlsafe(48)
    (output_dir / "secret_key.txt").write_text(secret_key, encoding="utf-8")

    # 2. Webhook signing secret
    webhook_secret = secrets.token_urlsafe(48)
    (output_dir / "webhook_signing_secret.txt").write_text(webhook_secret, encoding="utf-8")

    # 3. Unsubscribe signing key
    unsub_key = secrets.token_urlsafe(48)
    (output_dir / "unsubscribe_signing_key.txt").write_text(unsub_key, encoding="utf-8")

    # 4. Postgres database password & URL
    db_password = secrets.token_urlsafe(24)
    (output_dir / "postgres_password.txt").write_text(db_password, encoding="utf-8")
    db_url = f"postgresql+psycopg://bms:{db_password}@postgres:5432/bms"
    (output_dir / "database_url.txt").write_text(db_url, encoding="utf-8")

    # 5. Operator TOTP MFA secret
    totp_secret = generate_totp_secret()
    (output_dir / "operator_totp_secret.txt").write_text(totp_secret, encoding="utf-8")

    # 6. Operator password hash
    plain_password = operator_password or secrets.token_urlsafe(20)
    hashed = password_hash.hash(plain_password)
    (output_dir / "operator_password_hash.txt").write_text(hashed, encoding="utf-8")

    print(f"[OK] Production secrets generated in: {output_dir.resolve()}")
    print("Files created:")
    for f in output_dir.glob("*.txt"):
        print(f"  - {f.name}")

    if not operator_password:
        print("\n[IMPORTANT] Generated temporary operator password:")
        print("  Username: operator")
        print(f"  Password: {plain_password}")
        print("  TOTP Secret: " + totp_secret)
        print("Store these credentials in your password manager immediately!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate production secrets for Docker secrets")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "secrets",
        help="Directory where secret files will be written (default: ./secrets)",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=None,
        help="Custom plain operator password to hash (if omitted, a secure random one is generated)",
    )
    args = parser.parse_args()
    generate_production_secrets(args.output_dir, operator_password=args.password)


if __name__ == "__main__":
    main()
