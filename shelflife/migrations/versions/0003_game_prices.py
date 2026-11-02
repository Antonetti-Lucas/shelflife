"""Games remember their store price

Revision ID: 0003
Revises: 0002
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database from before migrations gains these when brought to the baseline (see 0002).
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("games")}
    with op.batch_alter_table("games") as batch:
        if "price" not in columns:
            batch.add_column(sa.Column("price", sa.JSON(), nullable=True))
        if "price_fetched_at" not in columns:
            batch.add_column(sa.Column("price_fetched_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.drop_column("price_fetched_at")
        batch.drop_column("price")
