"""normalize campaign recipients into relational table
Revision ID: 0008_normalize_campaign_recipients
Revises: 0007_manual_retention
Create Date: 2026-11-13 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_normalize_campaign_recipients"
down_revision = "0007_manual_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # 1. Install pgcrypto extension for native HMAC-SHA256 SQL pushdown in PostgreSQL
    if not is_sqlite:
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # 2. Create relational campaign_recipients junction table
    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("consent_record_id", sa.Integer(), nullable=True),
        sa.Column("unsubscribe_token", sa.String(length=180), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("outbox_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["consent_record_id"], ["consent_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["outbox_id"], ["outbox_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "email_normalized", name="uq_campaign_recipient_email"),
    )

    # 3. High-performance composite and relational indexes
    op.create_index(
        "ix_campaign_recipients_campaign_status",
        "campaign_recipients",
        ["campaign_id", "status"],
    )
    op.create_index("ix_campaign_recipients_contact", "campaign_recipients", ["contact_id"])
    op.create_index("ix_campaign_recipients_email", "campaign_recipients", ["email_normalized"])
    op.create_index("ix_campaign_recipients_outbox", "campaign_recipients", ["outbox_id"])

    # 4. Standalone index on campaign_events.created_at to fix full table scan
    op.create_index("ix_campaign_events_created_at", "campaign_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_campaign_events_created_at", table_name="campaign_events")
    op.drop_index("ix_campaign_recipients_outbox", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_email", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_contact", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_campaign_status", table_name="campaign_recipients")
    op.drop_table("campaign_recipients")
