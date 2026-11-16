"""Add staged row snapshot and one-time commit token to import batches."""

import sqlalchemy as sa
from alembic import op

revision = "0003_import_review"
down_revision = "0002_operator_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_batches", sa.Column("commit_token_hash", sa.String(64), nullable=True))
    op.add_column("import_batches", sa.Column("rows_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "import_batches", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE import_batches SET commit_token_hash = '', rows_snapshot = '[]', expires_at = CURRENT_TIMESTAMP WHERE commit_token_hash IS NULL"
    )
    with op.batch_alter_table("import_batches") as batch:
        batch.alter_column("commit_token_hash", nullable=False)
        batch.alter_column("rows_snapshot", nullable=False)
        batch.alter_column("expires_at", nullable=False)


def downgrade() -> None:
    op.drop_column("import_batches", "rows_snapshot")
    op.drop_column("import_batches", "expires_at")
    op.drop_column("import_batches", "commit_token_hash")
