"""add bounced and complained to outbox_status enum and add index on outbox_messages.email_normalized

Revision ID: 0009_outbox_bounce_complaint_status
Revises: 0008_normalize_campaign_recipients
Create Date: 2026-11-14 10:00:00.000000
"""

from alembic import op

revision = "0009_outbox_bounce_complaint_status"
down_revision = "0008_normalize_campaign_recipients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Extend PostgreSQL outbox_status enum with BOUNCED and COMPLAINED
    if is_postgres:
        op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'BOUNCED';")
        op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'COMPLAINED';")

    # 2. Add standalone index on outbox_messages(email_normalized) for fast multi-campaign state sync
    op.create_index(
        "ix_outbox_messages_email_normalized",
        "outbox_messages",
        ["email_normalized"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_email_normalized", table_name="outbox_messages")
