"""Baseline: the tables as they were when migrations began

Revision ID: 0001
Revises: 
Created: 2026-11-01 01:18:48.646026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The six tables as they were on 2026-11-01, when migrations began.
    op.create_table('games',
    sa.Column('steam_app_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('type', sa.String(), nullable=True),
    sa.Column('short_description', sa.String(), nullable=True),
    sa.Column('about_text', sa.Text(), nullable=True),
    sa.Column('genres', sa.JSON(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('release_date', sa.String(), nullable=True),
    sa.Column('cover_url', sa.String(), nullable=True),
    sa.Column('details_fetched_at', sa.DateTime(), nullable=True),
    sa.Column('tags_fetched_at', sa.DateTime(), nullable=True),
    sa.Column('reviews_positive', sa.Integer(), nullable=True),
    sa.Column('reviews_negative', sa.Integer(), nullable=True),
    sa.Column('reviews_fetched_at', sa.DateTime(), nullable=True),
    sa.Column('player_hours', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('player_hours_sample', sa.Integer(), nullable=True),
    sa.Column('player_hours_fetched_at', sa.DateTime(), nullable=True),
    sa.Column('family_shared', sa.Boolean(), nullable=True),
    sa.PrimaryKeyConstraint('steam_app_id')
    )
    op.create_table('settings',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('diary_entries',
    sa.Column('steam_app_id', sa.Integer(), nullable=False),
    sa.Column('rating', sa.Double(), nullable=True),
    sa.Column('not_interested', sa.Boolean(), nullable=False),
    sa.Column('review', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('started_on', sa.Date(), nullable=True),
    sa.Column('finished_on', sa.Date(), nullable=True),
    sa.Column('favorite', sa.Boolean(), nullable=True),
    sa.ForeignKeyConstraint(['steam_app_id'], ['games.steam_app_id'], ),
    sa.PrimaryKeyConstraint('steam_app_id')
    )
    op.create_table('library_entries',
    sa.Column('steam_app_id', sa.Integer(), nullable=False),
    sa.Column('playtime_total_min', sa.Integer(), nullable=False),
    sa.Column('playtime_recent_min', sa.Integer(), nullable=False),
    sa.Column('last_played', sa.DateTime(), nullable=True),
    sa.Column('last_synced', sa.DateTime(), nullable=False),
    sa.Column('achievements_total', sa.Integer(), nullable=True),
    sa.Column('achievements_unlocked', sa.Integer(), nullable=True),
    sa.Column('rarest_unlocked_pct', sa.Double(), nullable=True),
    sa.Column('first_unlock_at', sa.DateTime(), nullable=True),
    sa.Column('last_unlock_at', sa.DateTime(), nullable=True),
    sa.Column('unlock_times', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('achievements_fetched_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['steam_app_id'], ['games.steam_app_id'], ),
    sa.PrimaryKeyConstraint('steam_app_id')
    )
    op.create_table('playtime_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('steam_app_id', sa.Integer(), nullable=False),
    sa.Column('taken_at', sa.DateTime(), nullable=False),
    sa.Column('playtime_total_min', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['steam_app_id'], ['games.steam_app_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('playtime_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_playtime_snapshots_steam_app_id'), ['steam_app_id'], unique=False)

    op.create_table('wishlist_items',
    sa.Column('steam_app_id', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=True),
    sa.Column('added_at', sa.DateTime(), nullable=True),
    sa.Column('seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['steam_app_id'], ['games.steam_app_id'], ),
    sa.PrimaryKeyConstraint('steam_app_id')
    )


def downgrade() -> None:
    # Undoes the baseline: every table goes.
    op.drop_table('wishlist_items')
    with op.batch_alter_table('playtime_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_playtime_snapshots_steam_app_id'))

    op.drop_table('playtime_snapshots')
    op.drop_table('library_entries')
    op.drop_table('diary_entries')
    op.drop_table('settings')
    op.drop_table('games')
