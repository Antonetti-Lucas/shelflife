"""Your wishlist teaches your taste weakly: each wishlisted game lifts its close look-alikes."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.learned import NearIndex, solve_sparse
from shelflife.models import WishlistItem
from shelflife.recommend import WANT_WEIGHT, _build_model, score_all
from shelflife.web.app import app
from tests.factories import add_game

STEALTH = {"Stealth": 100, "Ninja": 90, "Rogue": 80}


@pytest.fixture
def world(session):
    add_game(session, 1, "Loved", tags={"Zeta": 100, "Alpha": 50}, minutes=6000)
    add_game(session, 2, "Owned stealth", tags=dict(STEALTH))  # so these tags count in your library
    add_game(session, 3, "Owned farm", tags={"Farming": 100, "Cozy": 90})
    add_game(session, 10, "Wished", tags=dict(STEALTH), owned=False, reviews=(900, 100))
    add_game(session, 11, "Twin of wished", tags=dict(STEALTH), owned=False, reviews=(900, 100))
    add_game(session, 12, "Far away", tags={"Farming": 100, "Cozy": 90}, owned=False, reviews=(900, 100))
    diary.save_rating(session, 1, 5.0)
    return {1: 5.0}


def wish(session, *app_ids):
    for app_id in app_ids:
        session.add(WishlistItem(steam_app_id=app_id, priority=0, added_at=datetime(2026, 1, 1), seen_at=datetime(2026, 1, 1)))
    session.commit()


def liking(session, ratings):
    model = _build_model(session, ratings, {})
    return {i: model.liking(i) for i in (10, 11, 12)}


def test_a_wishlisted_game_lifts_its_look_alikes_only_and_never_itself(session, world):
    before = liking(session, world)
    wish(session, 10)
    after = liking(session, world)
    assert after[11] > before[11]  # its twin rises
    assert after[12] == pytest.approx(before[12])  # an unlike game doesn't
    assert after[10] == pytest.approx(before[10])  # and it doesn't lift itself


def test_a_rating_wins_over_the_wishlist(session, world):
    wish(session, 10)
    ratings = {**world, 10: 2.0}
    model = _build_model(session, ratings, {})
    assert 10 not in model.wanted and model.liking(11) < 0  # now a dislike: its twin goes down


def test_not_interested_wins_over_the_wishlist(session, world):
    wish(session, 10)
    model = _build_model(session, world, {10: "Wished"})
    assert 10 not in model.wanted and 10 in model.dismissed


def test_the_scores_notice_a_new_wishlist_without_anything_else_changing(session, world):
    before = score_all(session, world).by_id[11].score
    wish(session, 10)
    assert score_all(session, world).by_id[11].score > before


def test_many_wishlisted_twins_never_count_more_than_one_rating(session, world):
    for app_id in range(20, 30):  # ten more copies of the same game, all wished
        add_game(session, app_id, f"Copy {app_id}", tags=dict(STEALTH), owned=False, reviews=(900, 100))
    wish(session, 10)
    one = _build_model(session, world, {}).liking(11)
    wish(session, *range(20, 30))
    eleven = _build_model(session, world, {}).liking(11)
    assert one < eleven < WANT_WEIGHT  # fitted together: more than one, but never past a plain 3.5-star rating


def test_the_close_pairs_and_the_sparse_solve_match_the_full_ones():
    tags = {1: {"a": 1.0}, 2: {"a": 0.8, "b": 0.6}, 3: {"c": 1.0}}
    index = NearIndex([1, 2, 3], tags, {}, 0.0, 0.2)
    assert sorted(index.near(1)) == [(1, 1.0), (2, pytest.approx(0.8))]  # 3 shares nothing
    # [[4, 1], [1, 3]] x = [1, 2]  ->  x = [1/11, 7/11]
    assert solve_sparse([4.0, 3.0], [[(1, 1.0)], [(0, 1.0)]], [1.0, 2.0]) == [pytest.approx(1 / 11), pytest.approx(7 / 11)]


def test_game_pages_and_cards_offer_to_wishlist_on_steam(session, world):
    with TestClient(app) as client:
        page = client.get("/game/12").text
        assert "Wishlist it on Steam ↗" in page and 'href="https://store.steampowered.com/app/12/"' in page
        assert "Wishlist it on Steam" not in client.get("/game/1").text  # you own it
        wish(session, 12)
        assert "Wishlist it on Steam" not in client.get("/game/12").text  # already wishlisted
        explore = client.get("/explore").text
        assert 'class="wish-link" href="https://store.steampowered.com/app/11/"' in explore
        assert 'class="wish-link" href="https://store.steampowered.com/app/12/"' not in explore
