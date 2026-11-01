"""The database is found in the project folder, whichever folder ShelfLife is started from."""

import os
import subprocess
import sys
from pathlib import Path

from shelflife.db import PROJECT_FOLDER, default_database_url

PROJECT = Path(__file__).resolve().parents[1]


def test_the_default_database_is_in_the_project_folder():
    assert PROJECT_FOLDER == PROJECT
    assert default_database_url() == f"sqlite:///{(PROJECT / 'shelflife.db').as_posix()}"


def test_started_from_another_folder_it_still_points_at_the_project_folder(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = str(PROJECT)
    out = subprocess.run(
        [sys.executable, "-c", "import shelflife.db as d; print(d.DATABASE_URL)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == default_database_url()  # only reads the setting: no database is opened or created
    assert not list(tmp_path.iterdir())


def test_an_explicit_setting_still_wins(tmp_path):
    env = dict(os.environ, DATABASE_URL="sqlite:///demo.db", PYTHONPATH=str(PROJECT))
    out = subprocess.run(
        [sys.executable, "-c", "import shelflife.db as d; print(d.DATABASE_URL)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "sqlite:///demo.db"
