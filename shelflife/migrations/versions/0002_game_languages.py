"""Games remember the languages they can be played in

Revision ID: 0002
Revises: 0001
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database from before migrations already gained the column when it was brought to the
    # baseline (db._add_missing_columns adds every column the models have), so check first.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("games")}
    if "languages" not in columns:
        with op.batch_alter_table("games") as batch:
            batch.add_column(sa.Column("languages", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.drop_column("languages")
