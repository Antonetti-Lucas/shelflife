"""A game's lowest price ever on Steam, from IsThereAnyDeal, and its id there

Revision ID: 0011
Revises: 0010
Created: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("itad_id", sa.String()),
    ("price_low", sa.JSON()),
    ("price_low_fetched_at", sa.DateTime()),
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
