"""Games you played on other platforms: a library entry says where

Revision ID: 0006
Revises: 0005
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database from before migrations gains it when brought to the baseline (see 0002).
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("library_entries")}
    if "platform" not in columns:
        with op.batch_alter_table("library_entries") as batch:
            batch.add_column(sa.Column("platform", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_entries") as batch:
        batch.drop_column("platform")
