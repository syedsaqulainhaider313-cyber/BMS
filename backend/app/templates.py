from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import nh3
from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

UNSUBSCRIBE_TOKEN = "{{unsubscribe_url}}"
ALLOWED_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


def sanitize_html(value: str) -> str:
    cleaned = nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        strip_comments=True,
    )
    if cleaned.count(UNSUBSCRIBE_TOKEN) != 1:
        raise HTTPException(422, "HTML must include {{unsubscribe_url}} exactly once")
    return cleaned


def validate_text(value: str) -> str:
    if value.count(UNSUBSCRIBE_TOKEN) != 1:
        raise HTTPException(422, "Text body must include {{unsubscribe_url}} exactly once")
    if "\x00" in value:
        raise HTTPException(422, "Text body contains an invalid character")
    return value.strip()


class TemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    subject: str = Field(min_length=1, max_length=200)
    sender_name: str = Field(min_length=1, max_length=160)
    sender_email: str = Field(min_length=3, max_length=320)
    mailing_address: str = Field(min_length=5, max_length=1000)
    html_body: str = Field(min_length=1, max_length=100_000)
    text_body: str = Field(min_length=1, max_length=50_000)

    @field_validator("sender_email")
    @classmethod
    def valid_sender(cls, value: str) -> str:
        try:
            normalized = validate_email(value.strip(), check_deliverability=False).normalized
        except EmailNotValidError:
            raise ValueError("Enter a valid sender email address")
        return normalized


def template_values(payload: TemplateInput) -> dict[str, str]:
    return {
        **payload.model_dump(),
        "html_body": sanitize_html(payload.html_body),
        "text_body": validate_text(payload.text_body),
    }


def render_preview(payload: TemplateInput) -> dict[str, str]:
    values = template_values(payload)
    html = values["html_body"].replace(
        UNSUBSCRIBE_TOKEN, "https://example.test/unsubscribe/preview-token"
    )
    text = values["text_body"].replace(
        UNSUBSCRIBE_TOKEN, "https://example.test/unsubscribe/preview-token"
    )
    return {
        "subject": values["subject"],
        "sender": f"{values['sender_name']} <{values['sender_email']}>",
        "mailing_address": values["mailing_address"],
        "html_body": html,
        "text_body": text,
    }


def build_rfc_compliant_mime(
    *,
    from_name: str,
    from_address: str,
    to_address: str,
    subject: str,
    html_body: str,
    text_body: str,
    unsubscribe_token: str,
    domain: str,
    campaign_id: int,
    template_id: int | None = None,
    custom_mail_from_domain: str | None = None,
) -> bytes:
    """Constructs a strict RFC 5322 & RFC 8058 compliant multipart/alternative MIME message.

    Structure:
    - multipart/alternative
      - part 1: text/plain (utf-8)
      - part 2: text/html (utf-8)

    Headers Injected:
    - RFC 5322: From, To, Subject, Date (GMT), Message-ID
    - RFC 3834: Auto-Submitted: auto-generated
    - Deliverability: Precedence: bulk
    - Google Postmaster Tools: Feedback-ID
    - SPF Alignment: Return-Path (<bounces@bounces.domain.com>)
    - RFC 8058: List-Unsubscribe (<https://...>, <mailto:...>)
    - RFC 8058: List-Unsubscribe-Post: List-Unsubscribe=One-Click
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = f'"{from_name}" <{from_address}>'
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Reply-To"] = f"support@{domain}"
    msg["Auto-Submitted"] = "auto-generated"
    msg["Precedence"] = "bulk"

    # Custom Return-Path for SPF Alignment
    mail_from_subdomain = custom_mail_from_domain or f"bounces.{domain}"
    msg["Return-Path"] = f"<bounces@{mail_from_subdomain}>"

    # RFC 8058 One-Click List-Unsubscribe Headers
    unsub_https = f"https://{domain}/api/v1/unsubscribe/one-click/{unsubscribe_token}"
    unsub_mailto = f"mailto:unsubscribe@{domain}?subject=unsub_{unsubscribe_token}"
    msg["List-Unsubscribe"] = f"<{unsub_https}>, <{unsub_mailto}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Google Postmaster Tools Feedback-ID
    t_id = template_id if template_id is not None else 0
    msg["Feedback-ID"] = f"campaign-{campaign_id}:template-{t_id}:bms:{domain}"

    # Render body unsubscribe links
    rendered_text = text_body.replace(UNSUBSCRIBE_TOKEN, unsub_https)
    rendered_html = html_body.replace(UNSUBSCRIBE_TOKEN, unsub_https)

    # Attach Plain-Text part first (RFC 2046 requirement)
    part_text = MIMEText(rendered_text, "plain", "utf-8")
    msg.attach(part_text)

    # Attach HTML part second
    part_html = MIMEText(rendered_html, "html", "utf-8")
    msg.attach(part_html)

    return msg.as_bytes()

