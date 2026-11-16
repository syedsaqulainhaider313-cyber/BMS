"""Allow contact PII deletion while retaining campaign outcomes."""

from alembic import op

revision = "0007_manual_retention"
down_revision = "0006_mock_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("outbox_messages") as batch:
            batch.alter_column("contact_id", nullable=True)
            batch.create_foreign_key(
                "fk_outbox_contact_set_null",
                "contacts",
                ["contact_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        op.drop_constraint(
            "outbox_messages_contact_id_fkey", "outbox_messages", type_="foreignkey"
        )
        op.alter_column("outbox_messages", "contact_id", nullable=True)
        op.create_foreign_key(
            "fk_outbox_contact_set_null",
            "outbox_messages",
            "contacts",
            ["contact_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("outbox_messages") as batch:
            batch.drop_constraint("fk_outbox_contact_set_null", type_="foreignkey")
            batch.alter_column("contact_id", nullable=False)
            batch.create_foreign_key(
                "outbox_messages_contact_id_fkey",
                "contacts",
                ["contact_id"],
                ["id"],
                ondelete="RESTRICT",
            )
    else:
        op.drop_constraint(
            "fk_outbox_contact_set_null", "outbox_messages", type_="foreignkey"
        )
        op.alter_column("outbox_messages", "contact_id", nullable=False)
        op.create_foreign_key(
            "outbox_messages_contact_id_fkey",
            "outbox_messages",
            "contacts",
            ["contact_id"],
            ["id"],
            ondelete="RESTRICT",
        )
