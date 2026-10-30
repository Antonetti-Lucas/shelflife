"""Database engine and session setup."""

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///shelflife.db")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from shelflife import models  # noqa: F401  (registers tables on Base)

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all never alters existing tables; add columns new models gained.

    Stand-in for real migrations until the move to PostgreSQL.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    col_type = column.type.compile(engine.dialect)
                    conn.execute(
                        text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}')
                    )


@contextmanager
def session_scope() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
