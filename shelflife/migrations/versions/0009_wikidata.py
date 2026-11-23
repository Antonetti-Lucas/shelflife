"""What Wikidata knows about each game: its series and first release date

Revision ID: 0009
Revises: 0008
Created: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("wikidata", sa.JSON()),
    ("wikidata_fetched_at", sa.DateTime()),
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
