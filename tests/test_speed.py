"""Keeping the pages fast: a bounded number of queries per page, work kept between pages, and the
scores rebuilt in the background after a change."""

import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from shelflife import diary, recommend, similar
from shelflife.db import engine
from shelflife.models import Game, WishlistItem
from shelflife.recommend import _build_model
from shelflife.web.app import app
from tests.factories import add_game


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def many(session):
    """60 library games, 60 games you don't own, half of them wishlisted."""
    for i in range(1, 61):
        add_game(session, i, f"Owned {i}", tags={"Stealth": 100, f"T{i % 7}": 50}, minutes=120 * i)
    for i in range(100, 160):
        add_game(session, i, f"Pool {i}", tags={"Stealth": 90, f"T{i % 5}": 60}, owned=False, reviews=(900, 100))
        if i % 2:
            session.add(WishlistItem(steam_app_id=i, priority=0, added_at=datetime(2026, 1, 1), seen_at=datetime(2026, 1, 1)))
    session.commit()
    for i in range(1, 11):
        diary.save_rating(session, i, 4.5 if i % 2 else 2.0)
    return session


def queries_for(client, path):
    count = 0

    def counted(*args, **kwargs):
        nonlocal count
        count += 1

    client.get(path)  # build the scores first: this is about every later visit
    event.listen(engine, "before_cursor_execute", counted)
    try:
        assert client.get(path).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", counted)
    return count


@pytest.mark.parametrize("path", ["/library", "/explore", "/wishlist", "/", "/game/1", "/stats"])
def test_a_page_asks_the_database_a_few_times_not_once_per_game(client, many, path):
    # With 120 games, a page that asked once per game would make well over 100 queries.
    assert queries_for(client, path) < 30


def test_the_look_alike_search_is_kept_when_a_rating_doesnt_change_the_targets(session, many):
    ratings = diary.load_ratings(session)
    first = _build_model(session, ratings, {}).near_targets
    ratings[1] = 4.0  # was 4.5: still a like
    assert _build_model(session, ratings, {}).near_targets is first
    ratings[3] = 1.5  # a new dislike: a new search
    assert _build_model(session, ratings, {}).near_targets is not first


def test_tag_rarity_is_kept_until_some_games_tags_change(session, many):
    first = similar.tag_idf(session)
    assert similar.tag_idf(session) is first
    game = session.get(Game, 100)
    game.tags = {"Stealth": 90, "Brand new": 80}
    session.commit()
    assert "Brand new" in similar.tag_idf(session)


def test_a_change_rebuilds_the_scores_in_the_background(client, many, monkeypatch):
    built = threading.Event()
    real = recommend.score_all

    def score_all(*args, **kwargs):
        built.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(recommend, "score_all", score_all)
    monkeypatch.setattr(recommend, "WARM_AFTER_CHANGES", True)
    client.post("/game/1/rate", data={"rating": "3.5"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert built.wait(10)
    for thread in threading.enumerate():
        if thread.name == "warm-scores":
            thread.join(10)
