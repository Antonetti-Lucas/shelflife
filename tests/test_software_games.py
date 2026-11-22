"""Tools that Steam sells beside games (Blender, Aseprite): recognised by their store genres, and hidden from
recommendations and Explore unless you choose to see them."""

import pytest
from fastapi.testclient import TestClient

from shelflife.recommend import recommend, score_all
from shelflife.settings import get_hide_software, set_hide_software
from shelflife.tags import is_software_game
from shelflife.web.app import app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize(
    "genres, software",
    [
        (["Animation & Modeling", "Design & Illustration", "Video Production"], True),  # Blender
        (["Casual", "Indie", "Utilities"], True),  # Wallpaper Engine has game-like genres too
        (["Action", "Indie"], False),
        ([], False),  # genres unknown until the store details are read
        (None, False),
    ],
)
def test_the_store_genres_tell_tools_from_games(session, genres, software):
    game = add_game(session, 1, "Thing", tags={"Software": 100})
    game.genres = genres
    assert is_software_game(game) is software


@pytest.fixture
def world(session):
    add_game(session, 1, "Loved Stealth", tags={"Stealth": 100, "Singleplayer": 60}, minutes=900)
    tool = add_game(session, 3, "Pixel Editor", tags={"Stealth": 90, "Singleplayer": 50}, owned=False, reviews=(900, 100))
    tool.genres = ["Design & Illustration", "Utilities"]
    add_game(session, 4, "Real Game", tags={"Stealth": 90, "Singleplayer": 50}, owned=False, reviews=(900, 100))
    backlog = add_game(session, 5, "Paint Backlog", tags={"Stealth": 80, "Singleplayer": 50}, minutes=0)
    backlog.genres = ["Photo Editing"]
    session.commit()
    return {1: 5.0}


def names(recs):
    return {r.game.name for r in recs}


def test_they_are_hidden_by_default(session):
    assert get_hide_software(session) is True


def test_hidden_from_every_recommendation_list(session, world):
    result = recommend(session, world, limit=50)
    assert "Pixel Editor" not in names(result.discover) and "Paint Backlog" not in names(result.backlog)
    assert "Real Game" in names(result.discover)
    assert 3 not in score_all(session, world).discover


def test_shown_when_you_choose(session, world):
    set_hide_software(session, False)
    result = recommend(session, world, limit=50)
    assert {"Pixel Editor", "Real Game"} <= names(result.discover) and "Paint Backlog" in names(result.backlog)


def test_explore_follows_the_setting_and_the_library_always_shows_your_games(client, session, world):
    assert "Pixel Editor" not in client.get("/explore").text
    assert "Paint Backlog" in client.get("/library").text
    client.post("/taste/hide-software", data={}, headers=SAME_SITE)  # an unticked box sends nothing
    assert get_hide_software(session) is False
    assert "Pixel Editor" in client.get("/explore").text


def test_the_taste_page_has_the_switch_on_by_default(client):
    html = client.get("/taste").text
    assert 'action="/taste/hide-software"' in html and "checked" in html.split('action="/taste/hide-software"')[1][:300]
