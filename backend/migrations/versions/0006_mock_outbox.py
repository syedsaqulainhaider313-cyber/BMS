"""Add idempotent mock outbox and deduplicated campaign events."""

import sqlalchemy as sa
from alembic import op

revision = "0006_mock_outbox"
down_revision = "0005_campaign_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = sa.Enum(
        "PENDING", "RETRY", "ACCEPTED", "DELIVERED", "FAILED", "SUPPRESSED", name="outbox_status"
    )
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("status", status, nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("campaign_id", "email_normalized", name="uq_outbox_campaign_recipient"),
    )
    op.create_index("ix_outbox_ready", "outbox_messages", ["status", "next_attempt_at"])
    op.create_table(
        "campaign_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(160), nullable=False, unique=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "outbox_id", sa.Integer(), sa.ForeignKey("outbox_messages.id", ondelete="RESTRICT")
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("detail", sa.String(300)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_campaign_events_campaign_time", "campaign_events", ["campaign_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_events_campaign_time", table_name="campaign_events")
    op.drop_table("campaign_events")
    op.drop_index("ix_outbox_ready", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    sa.Enum(name="outbox_status").drop(op.get_bind(), checkfirst=True)
