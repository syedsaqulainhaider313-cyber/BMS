"""Add reusable email templates."""

import sqlalchemy as sa
from alembic import op

revision = "0004_email_templates"
down_revision = "0003_import_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("sender_name", sa.String(160), nullable=False),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("mailing_address", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("email_templates")
