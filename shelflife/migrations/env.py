"""Alembic's entry point: runs the migrations in versions/ against ShelfLife's database.

init_db() (shelflife/db.py) runs them at start-up, passing its own connection. Developers make a new
migration after changing shelflife/models.py with (from the project folder):

    python -m alembic revision --autogenerate -m "what changed"

and then read the file it writes before keeping it.
"""

from alembic import context

from shelflife import models  # noqa: F401  (registers the tables)
from shelflife.db import Base, engine

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:
    # SQLite can't alter most of a table in place; "batch" mode copies the table instead.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with engine.begin() as own:
        _run(own)
