"""Add campaign drafts and immutable approval snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0005_campaign_review"
down_revision = "0004_email_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = sa.Enum("DRAFT", "APPROVED", "PAUSED", "CANCELLED", name="campaign_status")
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("email_templates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("consent_purpose", sa.String(300), nullable=False),
        sa.Column("status", status, nullable=False, server_default="DRAFT"),
        sa.Column("frozen_content", sa.JSON()),
        sa.Column("recipient_snapshot", sa.JSON()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("campaigns")
    sa.Enum(name="campaign_status").drop(op.get_bind(), checkfirst=True)
