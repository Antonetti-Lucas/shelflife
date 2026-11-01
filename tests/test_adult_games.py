"""Games for adults: recognised from their tags, and hidden from recommendations and Explore unless
you choose to see them."""

import pytest
from fastapi.testclient import TestClient

from shelflife.recommend import recommend, score_all
from shelflife.settings import get_hide_adult, set_hide_adult
from shelflife.tags import is_adult
from shelflife.web.app import app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}


def tags(*names):
    """Tags in this order, the first with the most votes."""
    return {name: 1000 - 100 * i for i, name in enumerate(names)}


@pytest.mark.parametrize(
    "game, adult",
    [
        (tags("Sexual Content", "Nudity", "Hentai", "Mature"), True),
        (tags("Visual Novel", "NSFW", "Dating Sim", "First-Person"), True),
        (tags("FPS", "Sexual Content", "Shooter", "Visual Novel", "Story Rich"), True),  # a joke-tagged visual novel
        (tags("Anime", "Cute", "Casual", "Puzzle", "Platformer", "Indie", "Hentai"), True),
        (tags("Sexual Content", "FPS", "Horror", "Singleplayer", "Puzzle"), False),  # one joke tag on a shooter
        (tags("Open World", "RPG", "Nudity", "Sci-fi", "Story Rich"), False),  # adult scenes, not an adult game
        (tags("RPG", "Story Rich", "Fantasy", "Choices Matter", "Sexual Content"), False),
        (tags("Roguelike", "Action", "Mythology"), False),
        ({}, False),
    ],
)
def test_what_counts_as_a_game_for_adults(game, adult):
    assert is_adult(game) is adult


@pytest.fixture
def world(session):
    add_game(session, 1, "Loved", tags={"Zeta": 100, "Visual Novel": 40}, minutes=6000)
    add_game(session, 2, "Pure", tags={"Zeta": 100, "Visual Novel": 50}, owned=False, reviews=(900, 100))
    add_game(session, 3, "Adult", tags=tags("Sexual Content", "Zeta", "Visual Novel", "Nudity"), owned=False, reviews=(900, 100))
    add_game(session, 4, "Adult Backlog", tags=tags("Hentai", "Zeta", "Nudity"))
    return {1: 5.0}


def names(recs):
    return {r.game.name for r in recs}


def test_they_are_hidden_by_default(session):
    assert get_hide_adult(session) is True


def test_hidden_from_every_recommendation_list(session, world):
    result = recommend(session, world, limit=50)
    assert "Adult" not in names(result.discover) and "Adult Backlog" not in names(result.backlog)
    assert "Pure" in names(result.discover)
    scores = score_all(session, world)
    assert 3 not in scores.discover and 4 not in scores.backlog  # nor counted as recommended in the library


def test_shown_when_you_choose(session, world):
    set_hide_adult(session, False)
    result = recommend(session, world, limit=50)
    assert {"Adult", "Pure"} <= names(result.discover) and "Adult Backlog" in names(result.backlog)


def test_explore_follows_the_setting_and_the_library_always_shows_your_games(client, session, world):
    assert "Adult" not in client.get("/explore").text.replace("Adult Backlog", "")
    assert "Adult Backlog" in client.get("/library").text
    client.post("/taste/hide-adult", data={"hide": "0"}, headers=SAME_SITE)
    assert get_hide_adult(session) is False
    assert ">Adult<" in client.get("/explore").text


def test_the_taste_page_has_the_switch(client):
    html = client.get("/taste").text
    assert 'action="/taste/hide-adult"' in html and "checked" in html.split('action="/taste/hide-adult"')[1][:400]


def test_unchecking_the_box_shows_them(client, session):
    client.post("/taste/hide-adult", data={}, headers=SAME_SITE)  # an unticked box sends nothing
    assert get_hide_adult(session) is False
    client.post("/taste/hide-adult", data={"hide": "1"}, headers=SAME_SITE)
    assert get_hide_adult(session) is True


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
