"""Add server-side operator sessions and audit events."""

import sqlalchemy as sa
from alembic import op

revision = "0002_operator_access"
down_revision = "0001_initial_data_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(100)),
        sa.Column("target_id", sa.String(100)),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("operator_sessions")
