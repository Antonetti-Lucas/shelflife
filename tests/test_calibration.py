"""Calibrated lists: the top of each list follows your mix of kinds of game, so one big interest can't
fill it, without ever promoting a poor fit."""

import pytest
from fastapi.testclient import TestClient

from shelflife import recommend
from shelflife.recommend import _build_model, calibrate, interest_mix, score_all
from shelflife.recommend import recommend as recommend_games
from shelflife.settings import set_learn_from_play
from shelflife.web.app import app
from tests.factories import add_game

FILLERS = ["Chess", "Racing", "Farming", "Sports", "Golf", "Fishing"]


@pytest.fixture
def two_loves(session):
    """You love stealth games (three of them) and platformers (one): about three to one."""
    for i, tag in enumerate(FILLERS, start=50):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)
    for i in range(1, 4):
        add_game(session, i, f"Stealth {i}", tags={"Stealth": 100, "Action": 40, f"Spy{i}": 30}, minutes=600)
    add_game(session, 4, "Platformer", tags={"Platformer": 100, "Pixel Graphics": 40, "Jump": 30}, minutes=600)
    for i in range(10, 22):  # many stealth games to discover, all fitting well
        add_game(session, i, f"New stealth {i}", tags={"Stealth": 100, "Action": 40, f"Spy{i % 3 + 1}": 30}, owned=False, reviews=(900, 100))
    for i in range(30, 34):  # a few platformers, fitting a bit less
        add_game(session, i, f"New platformer {i}", tags={"Platformer": 100, "Pixel Graphics": 40, "Jump": 25}, owned=False, reviews=(900, 100))
    set_learn_from_play(session, False)
    return {1: 5.0, 2: 5.0, 3: 5.0, 4: 5.0}


def kinds_in_top(recs, n=8):
    return sum(1 for r in recs[:n] if "Platformer" in r.game.tags)


def test_your_mix_follows_how_much_you_liked_each_kind(session, two_loves):
    mix = interest_mix(session, two_loves, {})
    assert mix["Stealth"] > mix["Platformer"] > 0
    assert sum(mix.values()) == pytest.approx(1.0)


def test_without_calibration_one_big_interest_fills_the_top(session, two_loves, monkeypatch):
    monkeypatch.setattr(recommend, "CALIBRATION", 0.0)
    discover = recommend_games(session, two_loves, limit=50).discover
    assert kinds_in_top(discover) == 0


def test_calibrated_the_top_holds_your_other_interest_too(session, two_loves):
    discover = recommend_games(session, two_loves, limit=50).discover
    assert 1 <= kinds_in_top(discover) <= 4  # some platformers, stealth still first in number


def test_calibration_only_reorders_never_adds_or_drops(session, two_loves):
    scores = score_all(session, two_loves)
    plain = sorted((scores.by_id[i] for i in scores.discover), key=lambda r: -r.score)
    model = _build_model(session, two_loves, {})
    calibrated = calibrate(plain, model)
    assert sorted(r.game.steam_app_id for r in calibrated) == sorted(r.game.steam_app_id for r in plain)


def test_a_poor_fit_is_never_promoted_for_variety(session, two_loves):
    add_game(session, 40, "Weak platformer", tags={"Platformer": 30, "Golf": 100, "Fishing": 90}, owned=False, reviews=(900, 100))
    scores = score_all(session, two_loves)
    plain = sorted((scores.by_id[i] for i in scores.discover), key=lambda r: -r.score)
    weak = scores.by_id[40]
    assert weak.score < 0.5 * plain[0].score
    model = _build_model(session, two_loves, {})
    top = calibrate(plain, model, places=8)[:8]
    assert weak not in top


def test_with_one_interest_the_order_is_the_score_order(session):
    for i, tag in enumerate(FILLERS, start=50):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)
    add_game(session, 1, "Stealth", tags={"Stealth": 100}, minutes=600)
    for i in range(10, 15):
        add_game(session, i, f"New {i}", tags={"Stealth": 100, "Action": 10 * i}, owned=False, reviews=(900, 100))
    set_learn_from_play(session, False)
    scores = score_all(session, {1: 5.0})
    plain = sorted((scores.by_id[i] for i in scores.discover), key=lambda r: -r.score)
    model = _build_model(session, {1: 5.0}, {})
    assert [r.game.steam_app_id for r in calibrate(plain, model)][:1] == [plain[0].game.steam_app_id]


def test_the_recommendations_page_shows_your_mix(session, two_loves):
    from shelflife import diary

    for app_id, rating in two_loves.items():
        diary.save_rating(session, app_id, rating)
    with TestClient(app) as client:
        html = client.get("/").text
    assert "follows the mix of games you like" in html and "Stealth" in html


# ---- every list ranked by fit follows your mix, not only the home page ----


def first_names(html, n):
    import re

    return re.findall(r'<h3><a href="/game/\d+">([^<]+)</a></h3>', html)[:n]


@pytest.fixture
def rated(session, two_loves):
    from shelflife import diary

    for app_id, rating in two_loves.items():
        diary.save_rating(session, app_id, rating)
    return two_loves


def test_explore_best_fit_follows_your_mix(session, rated, monkeypatch):
    with TestClient(app) as client:
        varied = first_names(client.get("/explore").text, 8)
        assert any(name.startswith("New platformer") for name in varied)
        assert len(varied) == 8
        monkeypatch.setattr(recommend, "CALIBRATION", 0.0)
        recommend.clear_cache()
        plain = first_names(client.get("/explore").text, 8)
        assert not any(name.startswith("New platformer") for name in plain)


def test_explore_other_sorts_are_left_alone(session, rated):
    with TestClient(app) as client:
        names = first_names(client.get("/explore?sort=name").text, 30)
    assert names == sorted(names, key=str.lower)


def test_the_wishlist_best_fit_follows_your_mix(session, rated):
    from datetime import datetime

    from shelflife.models import WishlistItem

    for app_id in list(range(10, 22)) + list(range(30, 34)):
        session.add(WishlistItem(steam_app_id=app_id, priority=0, seen_at=datetime(2026, 6, 1)))
    session.commit()
    with TestClient(app) as client:
        top = first_names(client.get("/wishlist").text, 8)
    assert any(name.startswith("New platformer") for name in top)


def test_the_library_best_recommendation_follows_your_mix(session, rated):
    for i in range(60, 72):  # owned, not played yet: the library's recommended games
        add_game(session, i, f"Owned stealth {i}", tags={"Stealth": 100, "Action": 40, f"Spy{i % 3 + 1}": 30}, reviews=(900, 100))
    for i in range(80, 84):
        add_game(session, i, f"Owned platformer {i}", tags={"Platformer": 100, "Pixel Graphics": 40, "Jump": 25}, reviews=(900, 100))
    import re

    with TestClient(app) as client:
        html = client.get("/library?sort=fit").text
    names = re.findall(r'<a href="/game/\d+">(Owned [^<]+)</a>', html)[:8]
    assert any(name.startswith("Owned platformer") for name in names)
