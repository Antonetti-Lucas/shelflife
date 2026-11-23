"""What Steam's store change list said about each game

Revision ID: 0008
Revises: 0007
Created: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("store_changed_at", sa.DateTime()),
    ("price_change_seen", sa.Integer()),
    ("price_change_read", sa.Integer()),
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
