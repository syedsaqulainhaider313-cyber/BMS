"""Email delivery provider interface and implementations.

Strictly encapsulates delivery mechanisms.
Provides MockEmailProvider for deterministic offline testing and
AmazonSESProvider for production AWS SES v2 delivery with dedicated IP pools,
custom MAIL FROM return-path, raw multi-part MIME RFC 5322 assembly, and
comprehensive ClientError exception classification.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderResult:
    """Outcome of an attempted delivery dispatch."""

    status: str  # "delivered", "accepted", "transient", "hard_bounce", "complaint", "paused", "failed"
    provider_message_id: str
    error_detail: str | None = None


class EmailProvider(Protocol):
    """Protocol that all delivery providers must implement."""

    def dispatch(
        self,
        *,
        email: str,
        subject: str,
        html_body: str,
        text_body: str,
        campaign_id: int,
        outbox_id: int,
        unsubscribe_token: str = "",
        from_name: str | None = None,
        from_address: str | None = None,
        template_id: int | None = None,
        **kwargs: Any,
    ) -> ProviderResult:
        ...


class MockEmailProvider:
    """Deterministic local mock provider.

    Simulates realistic email outcomes without external network connectivity.
    Outcome is determined deterministically by a SHA-256 hash of the recipient address.
    """

    def dispatch(
        self,
        *,
        email: str,
        subject: str,
        html_body: str,
        text_body: str,
        campaign_id: int,
        outbox_id: int,
        unsubscribe_token: str = "",
        from_name: str | None = None,
        from_address: str | None = None,
        template_id: int | None = None,
        **kwargs: Any,
    ) -> ProviderResult:
        # Deterministic outcome partition (29 buckets)
        value = int(hashlib.sha256(email.encode("utf-8")).hexdigest()[:8], 16) % 29
        mock_msg_id = f"mock-msg-{campaign_id}-{outbox_id}-{value:04x}"

        if value == 0:
            return ProviderResult(
                status="hard_bounce",
                provider_message_id=mock_msg_id,
                error_detail="5.1.1 User unknown (simulated hard bounce)",
            )
        if value == 1:
            return ProviderResult(
                status="complaint",
                provider_message_id=mock_msg_id,
                error_detail="Feedback loop spam complaint received (simulated)",
            )
        if value == 2:
            return ProviderResult(
                status="transient",
                provider_message_id=mock_msg_id,
                error_detail="4.4.1 Connection timed out (simulated transient)",
            )
        return ProviderResult(
            status="delivered",
            provider_message_id=mock_msg_id,
            error_detail=None,
        )


class AmazonSESProvider:
    """Production email provider dispatching via Amazon SES v2 API.

    Features:
    - Dedicated IP pool selection via ConfigurationSetName.
    - Custom Return-Path alignment for SPF/DMARC compliance.
    - Multi-part MIME raw message dispatch.
    - Adaptive retry policy and persistent HTTP connection pooling.
    - ClientError classification into transient, hard_bounce, paused, and failed.
    """

    def __init__(self, client: Any = None) -> None:
        if client is not None:
            self.client = client
            return

        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise RuntimeError(
                "boto3 and botocore are required for AmazonSESProvider. "
                "Install boto3 to enable AWS SES sending."
            )

        boto_config = Config(
            region_name=getattr(settings, "aws_region", "us-east-1"),
            retries={"max_attempts": 3, "mode": "adaptive"},
            max_pool_connections=max(
                25, getattr(settings, "db_pool_size", 10) + getattr(settings, "db_max_overflow", 15)
            ),
            connect_timeout=5.0,
            read_timeout=10.0,
        )

        client_kwargs: dict[str, Any] = {
            "service_name": "sesv2",
            "region_name": getattr(settings, "aws_region", "us-east-1"),
            "config": boto_config,
        }
        if getattr(settings, "aws_access_key_id", None) and getattr(
            settings, "aws_secret_access_key", None
        ):
            client_kwargs["aws_access_key_id"] = settings.aws_access_key_id
            client_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
            if getattr(settings, "aws_session_token", None):
                client_kwargs["aws_session_token"] = settings.aws_session_token

        self.client = boto3.client(**client_kwargs)

    def dispatch(
        self,
        *,
        email: str,
        subject: str,
        html_body: str,
        text_body: str,
        campaign_id: int,
        outbox_id: int,
        unsubscribe_token: str = "",
        from_name: str | None = None,
        from_address: str | None = None,
        template_id: int | None = None,
        **kwargs: Any,
    ) -> ProviderResult:
        from app.templates import build_rfc_compliant_mime

        sender_name = from_name or settings.mail_from_name
        sender_email = from_address or settings.mail_from_address
        sender_domain = sender_email.split("@")[-1] if "@" in sender_email else "example.test"

        custom_mail_from = getattr(settings, "custom_mail_from_domain", None) or getattr(
            settings, "mail_from_domain", f"bounces.{sender_domain}"
        )

        # 1. Assemble RFC 5322 & RFC 8058 compliant MIME byte payload
        raw_mime = build_rfc_compliant_mime(
            from_name=sender_name,
            from_address=sender_email,
            to_address=email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            unsubscribe_token=unsubscribe_token,
            domain=sender_domain,
            campaign_id=campaign_id,
            template_id=template_id,
            custom_mail_from_domain=custom_mail_from,
        )

        # 2. Prepare SES v2 send_email request
        send_kwargs: dict[str, Any] = {
            "FromEmailAddress": f'"{sender_name}" <{sender_email}>',
            "Destination": {"ToAddresses": [email]},
            "Content": {"Raw": {"Data": raw_mime}},
            "EmailTags": [
                {"Name": "campaign_id", "Value": str(campaign_id)},
                {"Name": "outbox_id", "Value": str(outbox_id)},
            ],
        }

        # Dedicated IP Pool attachment via Configuration Set
        if getattr(settings, "ses_configuration_set", None):
            send_kwargs["ConfigurationSetName"] = settings.ses_configuration_set

        # 3. Execute SES API call with comprehensive error classification
        try:
            response = self.client.send_email(**send_kwargs)
            message_id = response.get("MessageId", f"ses-{campaign_id}-{outbox_id}")
            return ProviderResult(status="accepted", provider_message_id=message_id)

        except Exception as e:
            # Check for botocore ClientError or duck-typed error response
            response_dict = getattr(e, "response", {}) or {}
            error_dict = response_dict.get("Error", {})
            error_code = error_dict.get("Code", type(e).__name__)
            error_message = error_dict.get("Message", str(e))

            logger.error(
                "ses_dispatch_failed: code=%s message=%s outbox_id=%d",
                error_code,
                error_message,
                outbox_id,
            )

            # A. Throttles / AWS Quota exceeded -> Transient retry
            if error_code in {"TooManyRequestsException", "LimitExceededException", "ThrottlingException"}:
                return ProviderResult(
                    status="transient",
                    provider_message_id="",
                    error_detail=f"throttled:{error_code}",
                )

            # B. Account or sending paused -> Operational halt
            if error_code in {
                "AccountSuspendedException",
                "SendingPausedException",
                "AccountSendingPausedException",
            }:
                return ProviderResult(
                    status="paused",
                    provider_message_id="",
                    error_detail=f"account_paused:{error_code}",
                )

            # C. Recipient or domain hard rejection -> Hard bounce suppression
            if error_code in {"MessageRejected", "MailFromDomainNotVerifiedException"}:
                return ProviderResult(
                    status="hard_bounce",
                    provider_message_id="",
                    error_detail=f"rejected:{error_message}",
                )

            # D. Client error / payload malformed -> Failed
            if error_code in {"BadRequestException", "InvalidParameterValueException"}:
                return ProviderResult(
                    status="failed",
                    provider_message_id="",
                    error_detail=f"bad_request:{error_message}",
                )

            # Default fallback for other errors
            return ProviderResult(
                status="transient",
                provider_message_id="",
                error_detail=f"{error_code}:{error_message}",
            )


def get_email_provider(name: str = "mock") -> EmailProvider:
    """Factory returning the configured email provider."""
    if name == "mock":
        return MockEmailProvider()
    if name == "ses":
        return AmazonSESProvider()
    raise RuntimeError(
        f"Provider '{name}' is not supported. Supported: 'mock', 'ses'."
    )
