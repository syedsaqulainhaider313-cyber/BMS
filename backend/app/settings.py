from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "local-only-change-me"
    secret_key_file: str | None = None
    unsubscribe_signing_key: str = "local-only-unsubscribe-key-change-me"
    unsubscribe_signing_key_file: str | None = None
    webhook_signing_secret: str = "local-only-webhook-secret-change-me"
    webhook_signing_secret_file: str | None = None
    operator_username: str = "operator"
    operator_password_hash: str = ""
    operator_password_hash_file: str | None = None
    operator_totp_secret: str = ""
    operator_totp_secret_file: str | None = None
    session_max_age_seconds: int = 28800
    secure_cookies: bool = False
    database_url: str = "postgresql+psycopg://bms:bms_local_only@localhost:5432/bms"
    database_url_file: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    email_provider: str = "mock"
    mail_from_name: str = "Bulk Mail Manager Local"
    mail_from_address: str = "operator@example.test"
    reply_to_address: str | None = None
    custom_mail_from_domain: str | None = "bounces.example.test"
    mail_from_domain: str = "bounces.example.test"

    # AWS SES & SNS Configuration
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    ses_configuration_set: str | None = None
    ses_ip_pool: str | None = None
    sns_verify_signature: bool = True

    # Database Pool Sizing
    db_pool_size: int = 10
    db_max_overflow: int = 15
    db_pool_recycle: int = 1800
    db_pool_timeout: int = 30

    # ISP Rate Throttling
    isp_rate_google: float = 20.0
    isp_burst_google: int = 40
    isp_rate_yahoo: float = 10.0
    isp_burst_yahoo: int = 20
    isp_rate_microsoft: float = 10.0
    isp_burst_microsoft: int = 20
    isp_rate_apple: float = 5.0
    isp_burst_apple: int = 10
    isp_rate_default: float = 10.0
    isp_burst_default: int = 20

    max_sends_per_minute: int = 10
    max_campaign_recipients: int = 50000

    @model_validator(mode="before")
    @classmethod
    def load_file_based_secrets(cls, data: Any) -> Any:
        if isinstance(data, dict):
            mapping = {
                "secret_key": "secret_key_file",
                "unsubscribe_signing_key": "unsubscribe_signing_key_file",
                "webhook_signing_secret": "webhook_signing_secret_file",
                "operator_password_hash": "operator_password_hash_file",
                "operator_totp_secret": "operator_totp_secret_file",
                "database_url": "database_url_file",
            }
            for target_field, file_field in mapping.items():
                file_path = data.get(file_field)
                if file_path:
                    p = Path(file_path)
                    if p.is_file():
                        data[target_field] = p.read_text(encoding="utf-8").strip()
        return data

    @model_validator(mode="after")
    def validate_runtime_configuration(self):
        if self.app_env not in {"development", "test", "staging", "production"}:
            raise ValueError("APP_ENV must be development, test, staging, or production")
        if self.email_provider not in {"mock", "ses"}:
            raise ValueError(f"EMAIL_PROVIDER must be 'mock' or 'ses', got '{self.email_provider}'")
        if self.session_max_age_seconds < 300 or self.session_max_age_seconds > 86400:
            raise ValueError("SESSION_MAX_AGE_SECONDS must be between 300 and 86400")
        if self.max_sends_per_minute < 1 or self.max_campaign_recipients < 1:
            raise ValueError("Sending caps must be positive")
        if self.app_env == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if (
                len(self.secret_key) < 32
                or self.secret_key.startswith("replace-with-")
                or self.secret_key == "local-only-change-me"
            ):
                raise ValueError(
                    "Production SECRET_KEY must be a unique value of at least 32 characters"
                )
            if not self.operator_password_hash:
                raise ValueError("OPERATOR_PASSWORD_HASH must be configured in production")
            if not self.secure_cookies:
                raise ValueError("SECURE_COOKIES must be true in production")
            if (
                len(self.unsubscribe_signing_key) < 32
                or self.unsubscribe_signing_key.startswith("replace-with-")
                or self.unsubscribe_signing_key == "local-only-unsubscribe-key-change-me"
            ):
                raise ValueError(
                    "UNSUBSCRIBE_SIGNING_KEY must be unique, stable, and at least 32 characters"
                )
            if (
                len(self.webhook_signing_secret) < 32
                or self.webhook_signing_secret.startswith("replace-with-")
                or self.webhook_signing_secret == "local-only-webhook-secret-change-me"
            ):
                raise ValueError(
                    "WEBHOOK_SIGNING_SECRET must be unique, stable, and at least 32 characters"
                )
        if self.operator_totp_secret:
            import base64

            clean = self.operator_totp_secret.strip().replace(" ", "").upper()
            missing = len(clean) % 8
            if missing:
                clean += "=" * (8 - missing)
            try:
                decoded = base64.b32decode(clean, casefold=True)
                if len(decoded) < 10:
                    raise ValueError("OPERATOR_TOTP_SECRET too short")
            except Exception as exc:
                raise ValueError("OPERATOR_TOTP_SECRET must be a valid Base32 secret") from exc
        return self

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        secrets_dir="/run/secrets" if Path("/run/secrets").is_dir() else None,
        extra="ignore",
    )


settings = Settings()

if settings.email_provider not in {"mock", "ses"}:
    raise RuntimeError(
        f"Only EMAIL_PROVIDER in ('mock', 'ses') is available in this scaffold, got '{settings.email_provider}'"
    )
