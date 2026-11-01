"""Database engine and session setup."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

PROJECT_FOLDER = Path(__file__).resolve().parents[1]


def default_database_url() -> str:
    """shelflife.db in the project folder, wherever you start ShelfLife from. A relative path
    would be read from the folder you happen to be in, and starting there would quietly create
    a new, empty database: your diary would seem to be gone."""
    return f"sqlite:///{(PROJECT_FOLDER / 'shelflife.db').as_posix()}"


DATABASE_URL = os.getenv("DATABASE_URL") or default_database_url()

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


MIGRATIONS = Path(__file__).resolve().parent / "migrations"
BASELINE = "0001"  # the schema as it was when migrations began (migrations/versions/0001_baseline.py)


def _alembic(connection):
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    return config


def init_db() -> None:
    """Bring the database up to date with the code, by its migrations (shelflife/migrations).

    - A new, empty database gets every table, marked as up to date.
    - A database from before migrations (it has the tables but no version) is first brought to the
      baseline the old way, adding any columns it lacks, then marked as the baseline and upgraded.
    - Otherwise the migrations it hasn't had yet are applied.
    """
    from alembic import command

    from shelflife import models  # noqa: F401  (registers tables on Base)

    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        config = _alembic(conn)
        if "games" not in tables:
            # Empty (or only a stale version left behind): build the current schema and mark it.
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
            Base.metadata.create_all(conn)
            command.stamp(config, "head")
            return
        if "alembic_version" not in tables:
            _add_missing_columns(conn)
            command.stamp(config, BASELINE)
        command.upgrade(config, "head")


def _add_missing_columns(conn) -> None:
    """How databases were kept up to date before migrations: create missing tables and add the
    columns the models gained. Only used once, to bring an older database to the baseline."""
    Base.metadata.create_all(conn)
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing:
                col_type = column.type.compile(conn.dialect)
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
