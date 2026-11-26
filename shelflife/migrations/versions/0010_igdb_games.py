"""Games that are not on Steam, from IGDB: what IGDB knows about a game, and whether you edited its tags

Revision ID: 0010
Revises: 0009
Created: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("igdb", sa.JSON()),
    ("igdb_fetched_at", sa.DateTime()),
    ("tags_edited", sa.Boolean()),
)


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("games")}
    with op.batch_alter_table("games") as batch:
        for name, kind in COLUMNS:
            if name not in have:
                batch.add_column(sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        for name, _ in COLUMNS:
            batch.drop_column(name)
