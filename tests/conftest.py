"""Test setup: a temporary database and working folder.

Nothing here can touch your real data: the database lives in a temp directory,
every test runs with that temp directory as its working folder (so relative paths
like data/ratings.csv stay inside it), and no test makes a network call.
"""

import os
import tempfile
from pathlib import Path

# This has to happen before shelflife is imported anywhere: db.py reads
# DATABASE_URL once, when it is first imported.
_TMP = Path(tempfile.mkdtemp(prefix="shelflife-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
# Empty values keep python-dotenv from loading the real ones from a .env in the project folder.
os.environ["STEAM_API_KEY"] = ""
os.environ["STEAM_ID"] = ""

import pytest  # noqa: E402

from shelflife import achievements, appnames, learned, reviews, store  # noqa: E402
from shelflife.db import Base, SessionLocal, engine, init_db  # noqa: E402

if "shelflife-tests-" not in str(engine.url):  # pragma: no cover - safety net
    raise RuntimeError(f"Tests must not use a real database: {engine.url}")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fresh empty database and a private working folder for every test."""
    monkeypatch.chdir(tmp_path)
    Base.metadata.drop_all(engine)
    init_db()
    # What was learned from one test's games must not leak into the next one's.
    learned._text_cache.update(key=None, vectors={})
    learned._fit_cache.update(key=None, value=None)
    yield


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Throttles exist to be polite to real servers; tests never wait for them."""
    for throttle in (
        store.store_throttle,
        store.steamspy_throttle,
        reviews.reviews_throttle,
        appnames.app_list_throttle,
        achievements.steam_throttle,
        store.steamspy_all_throttle,
    ):
        monkeypatch.setattr(throttle, "min_interval", 0)


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


class FakeResponse:
    """Just enough of requests.Response for the code under test."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")
