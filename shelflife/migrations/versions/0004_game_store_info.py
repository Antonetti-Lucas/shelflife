"""Games keep more of their store details (developers, categories, platforms...)

Revision ID: 0004
Revises: 0003
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database from before migrations gains it when brought to the baseline (see 0002).
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("games")}
    if "store_info" not in columns:
        with op.batch_alter_table("games") as batch:
            batch.add_column(sa.Column("store_info", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.drop_column("store_info")
