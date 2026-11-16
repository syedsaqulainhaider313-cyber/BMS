"""Initial contacts, consent, suppression, and import foundation."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_data_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    suppression_reason = sa.Enum(
        "UNSUBSCRIBE", "HARD_BOUNCE", "COMPLAINT", "MANUAL", name="suppression_reason"
    )
    import_status = sa.Enum("PREVIEWED", "COMMITTED", "REJECTED", name="import_status")
    suppression_reason.create(op.get_bind(), checkfirst=True)
    import_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_contacts_email_normalized", "contacts", ["email_normalized"], unique=True)
    op.create_table(
        "consent_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source", sa.String(300), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.String(300), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "contact_id", "source", "consented_at", "purpose", name="uq_consent_evidence"
        ),
    )
    op.create_index("ix_consent_contact_time", "consent_records", ["contact_id", "consented_at"])
    op.create_table(
        "suppressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("reason", suppression_reason, nullable=False),
        sa.Column("source", sa.String(300), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_suppressions_email_normalized", "suppressions", ["email_normalized"], unique=True
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("status", import_status, nullable=False, server_default="PREVIEWED"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("import_batches")
    op.drop_index("ix_suppressions_email_normalized", table_name="suppressions")
    op.drop_table("suppressions")
    op.drop_index("ix_consent_contact_time", table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_index("ix_contacts_email_normalized", table_name="contacts")
    op.drop_table("contacts")
    sa.Enum(name="import_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="suppression_reason").drop(op.get_bind(), checkfirst=True)
