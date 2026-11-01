"""Database migrations: a new database, a database from before migrations, and the migrations
themselves agreeing with the models."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from shelflife import db
from shelflife.db import BASELINE, Base


@pytest.fixture
def fresh_engine(tmp_path, monkeypatch):
    """init_db() pointed at a database file of its own."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'other.db').as_posix()}")
    monkeypatch.setattr(db, "engine", engine)
    yield engine
    engine.dispose()


def version(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head() -> str:
    with db.engine.connect() as conn:
        return ScriptDirectory.from_config(db._alembic(conn)).get_current_head()


def test_a_new_database_gets_every_table_and_the_latest_version(fresh_engine):
    db.init_db()
    assert set(Base.metadata.tables) <= set(inspect(fresh_engine).get_table_names())
    assert version(fresh_engine) == head()


def test_running_it_again_changes_nothing(fresh_engine):
    db.init_db()
    db.init_db()
    assert version(fresh_engine) == head()


def test_a_database_from_before_migrations_keeps_its_data_and_gains_what_it_lacked(fresh_engine):
    with fresh_engine.begin() as conn:  # an old database: games without the newer columns, no version
        conn.execute(text("CREATE TABLE games (steam_app_id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, genres JSON NOT NULL, tags JSON NOT NULL)"))
        conn.execute(text("INSERT INTO games VALUES (620, 'Portal 2', '[]', '{\"Puzzle\": 10}')"))
    db.init_db()
    columns = {c["name"] for c in inspect(fresh_engine).get_columns("games")}
    assert {"about_text", "player_hours", "family_shared"} <= columns
    with fresh_engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM games")).scalar() == "Portal 2"
    assert version(fresh_engine) == head()
    assert "wishlist_items" in inspect(fresh_engine).get_table_names()  # missing tables are created too


def test_the_migrations_build_exactly_what_the_models_describe(fresh_engine):
    with fresh_engine.begin() as conn:
        command.upgrade(db._alembic(conn), "head")  # every migration, from nothing
    with fresh_engine.connect() as conn:
        differences = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert differences == []  # change a model? add a migration: python -m alembic revision --autogenerate


def test_the_baseline_is_the_first_migration():
    with db.engine.connect() as conn:
        script = ScriptDirectory.from_config(db._alembic(conn))
        assert script.get_revision(BASELINE).down_revision is None
