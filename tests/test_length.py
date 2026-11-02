"""Game length, estimated from how long reviewers played (shelflife/length.py): the estimate, the
light sample Sync reads for games you haven't played, and the length filter."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shelflife import reviews, sync
from shelflife.length import LENGTH_FROM_REVIEWERS, Length, estimate, in_band, is_endless
from shelflife.models import Game
from shelflife.reviews import PlayerHours
from shelflife.web.app import app
from shelflife.wishlist import WishlistEntry, import_wishlist
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


def hours(median, sample=300):
    return SimpleNamespace(player_hours=[median / 4, median / 2, median, median * 2, median * 4],
                           player_hours_sample=sample, tags={"Story Rich": 100})


# ---- the estimate ----


def test_a_game_with_an_ending_is_its_reviewers_median_scaled_to_main_plus_extras():
    length = estimate(hours(24.8))
    assert length == Length(24.8 / LENGTH_FROM_REVIEWERS, False)
    assert length.short_text == "~20 h" and "to finish" in length.text


def test_an_endless_game_is_how_long_its_players_typically_play():
    game = hours(300)
    game.tags = {"Multiplayer": 500, "FPS": 400, "Shooter": 300}
    assert estimate(game) == Length(300, True) and "typically play" in estimate(game).text
    assert not is_endless({"Story Rich": 9, "RPG": 8, "Open World": 7, "Multiplayer": 1})  # only the top 3 count


def test_no_reviewers_hours_means_no_estimate():
    assert estimate(SimpleNamespace(player_hours=None, player_hours_sample=0, tags={})) is None
    assert estimate(SimpleNamespace(player_hours=[1, 2, 3, 4, 5], player_hours_sample=0, tags={})) is None


@pytest.mark.parametrize("hours_, band, inside", [(3, "short", True), (5, "short", False), (5, "medium", True),
                                                    (39.9, "long", True), (40, "huge", True), (3, "", True)])
def test_length_bands(hours_, band, inside):
    assert in_band(Length(hours_, False), band) is inside


def test_a_game_without_a_length_is_in_no_band():
    assert not in_band(None, "short") and in_band(None, "")


@pytest.mark.parametrize("value, text", [(0.5, "<1 h"), (1.5, "~1.5 h"), (12.4, "~12 h")])
def test_short_text(value, text):
    assert Length(value, False).short_text == text


# ---- Sync reads a light sample for games you haven't played ----


def test_games_you_havent_played_are_due_for_a_length_wishlist_first(session):
    add_game(session, 1, "Played", minutes=3000)
    add_game(session, 2, "Backlog")
    add_game(session, 3, "Pool, popular", owned=False, reviews=(9000, 100))
    add_game(session, 4, "Pool, small", owned=False, reviews=(50, 5))
    add_game(session, 5, "Wished", owned=False, reviews=(10, 1))
    add_game(session, 6, "Read lately", owned=False)
    import_wishlist(session, [WishlistEntry(5, 0, None)])
    session.get(Game, 6).player_hours_fetched_at = datetime.now()
    session.commit()
    assert [g.steam_app_id for g in sync.length_due(session)] == [5, 2, 3, 4]
    assert 6 in [g.steam_app_id for g in sync.length_due(session, everything=True)]


def test_played_games_get_five_pages_and_the_others_one(session, monkeypatch):
    add_game(session, 1, "Played", minutes=3000)
    add_game(session, 2, "Backlog")
    asked = {}
    monkeypatch.setattr(reviews, "fetch_player_hours", lambda app_id, pos=None, neg=None, pages=5: asked.setdefault(app_id, pages)
                        and PlayerHours([1.0, 2.0, 4.0, 8.0, 16.0], 100))
    assert sync.sync_player_hours(session) == 2
    assert asked == {1: reviews.PLAYER_PAGES, 2: sync.LENGTH_PAGES}
    assert session.get(Game, 2).player_hours == [1.0, 2.0, 4.0, 8.0, 16.0] and sync.length_due(session) == []


# ---- shown, and filtered by ----


@pytest.fixture
def client(session):
    build_scenario(session)
    short = session.get(Game, 40)  # Pool Stealth Good: about 2 h
    short.player_hours, short.player_hours_sample = [1.0, 2.0, 2.5, 5.0, 9.0], 200
    session.commit()  # Pool Stealth Bad (41): length unknown
    with TestClient(app) as c:
        yield c


def test_cards_and_game_pages_show_the_length(client):
    assert "~2.0 h" in client.get("/explore").text or "~2 h" in client.get("/explore").text
    assert "to finish" in client.get("/game/40").text


def test_the_length_filter_keeps_games_in_the_band_and_leaves_out_unknown_ones(client):
    html = client.get("/explore?length=short").text
    assert "Pool Stealth Good" in html and "Pool Stealth Bad" not in html
    assert "Pool Stealth Good" not in client.get("/explore?length=long").text
    assert 'value="short" selected' in html and "length=short" in html
    assert client.get("/explore?length=nonsense").status_code == 200
    assert client.get("/wishlist?length=short").status_code == 200
    assert client.get("/library?length=huge").status_code == 200
