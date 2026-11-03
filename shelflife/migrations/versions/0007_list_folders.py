"""Folders for lists

Revision ID: 0007
Revises: 0006
Created: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database from before migrations gains these when brought to the baseline (see 0002).
    inspector = sa.inspect(op.get_bind())
    if "list_folders" not in inspector.get_table_names():
        op.create_table('list_folders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    if "folder_id" not in {c["name"] for c in inspector.get_columns("game_lists")}:
        with op.batch_alter_table("game_lists") as batch:
            batch.add_column(sa.Column("folder_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_game_lists_folder", "list_folders", ["folder_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("game_lists") as batch:
        batch.drop_constraint("fk_game_lists_folder", type_="foreignkey")
        batch.drop_column("folder_id")
    op.drop_table('list_folders')
