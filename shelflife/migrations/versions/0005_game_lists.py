"""Lists of games you make and name

Revision ID: 0005
Revises: 0004
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database from before migrations gains these tables when brought to the baseline (see 0002).
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "game_lists" not in tables:
        op.create_table('game_lists',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('ranked', sa.Boolean(), nullable=False),
        sa.Column('show_ratings', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    if "game_list_entries" not in tables:
        op.create_table('game_list_entries',
        sa.Column('list_id', sa.Integer(), nullable=False),
        sa.Column('steam_app_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['list_id'], ['game_lists.id'], ),
        sa.ForeignKeyConstraint(['steam_app_id'], ['games.steam_app_id'], ),
        sa.PrimaryKeyConstraint('list_id', 'steam_app_id')
        )


def downgrade() -> None:
    op.drop_table('game_list_entries')
    op.drop_table('game_lists')
