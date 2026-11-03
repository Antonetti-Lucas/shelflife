"""Smaller page improvements: the game's length shown big, the ratings chart, Explore's orders,
the favorite heart and the Reviews page."""

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.models import Game
from shelflife.web.app import app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def test_a_games_length_is_shown_big_not_among_the_facts(client, session):
    add_game(session, 1, "Long game", tags={"RPG": 100})
    game = session.get(Game, 1)
    game.player_hours, game.player_hours_sample = [10.0, 20.0, 40.0, 60.0, 90.0], 250
    session.commit()
    html = client.get("/game/1").text
    assert re.search(r'class="hero-length"[^>]*>\s*<strong>~\d+ h</strong>', html)
    assert "to finish, with some extras" in html and "from 250 reviewers" in html
    facts = html[html.index('class="facts"'):html.index('class="hero-length"')]
    assert "to finish" not in facts


def test_the_ratings_chart_has_a_column_per_half_star_linking_to_those_games(client, session):
    add_game(session, 1, "A")
    add_game(session, 2, "B")
    diary.save_rating(session, 1, 4.5)
    diary.save_rating(session, 2, 4.5)
    html = client.get("/stats").text
    assert html.count('<a href="/ratings?rating=') == 10
    assert 'title="2 games rated ★★★★½"' in html and 'href="/ratings?rating=4.5"><i style="height: 100%"' in html


def test_explore_offers_the_match_alone_as_an_order(client, session):
    add_game(session, 1, "Loved", tags={"Stealth": 100}, minutes=6000)
    diary.save_rating(session, 1, 5.0)
    add_game(session, 2, "Close but mixed", tags={"Stealth": 100}, owned=False, reviews=(400, 600))
    add_game(session, 3, "Further but loved", tags={"Stealth": 60, "Puzzle": 100}, owned=False, reviews=(9900, 100))
    titles = lambda html: re.findall(r'<h3><a href="/game/\d+">([^<]+)</a></h3>', html)  # noqa: E731
    assert titles(client.get("/explore?sort=match").text)[0] == "Close but mixed"
    page = client.get("/explore").text
    assert "Best fit (match and reviews)" in page and "Highest match" in page
    assert "Best fit ranks by this times its reviews" in page


def test_the_heart_favorites_a_game_where_your_stars_are(client, session):
    add_game(session, 1, "Hades")
    diary.save_rating(session, 1, 5.0, "Perfect.")
    for path in ("/ratings", "/ratings?view=list", "/game/1", "/reviews"):
        assert 'action="/game/1/favorite" class="heart' in client.get(path).text, path
    post(client, "/game/1/favorite", value="1")
    assert 'class="heart small on"' in client.get("/ratings").text
    assert 'class="heart large on"' in client.get("/game/1").text


def test_the_reviews_page_lists_what_you_wrote_searchable_and_sorted(client, session):
    for app_id, name, rating, review in [(1, "Hades", 5.0, "Perfect run after run."), (2, "Celeste", 3.5, "Hard but fair."), (3, "Portal 2", 4.0, None)]:
        add_game(session, app_id, name)
        diary.save_rating(session, app_id, rating, review)
    page = client.get("/reviews").text
    assert "Perfect run after run." in page and "Hard but fair." in page and "Portal 2" not in page
    assert '<span class="count">2</span>' in page and 'href="/reviews" class="on"' in page
    assert "Celeste" not in client.get("/reviews?q=perfect").text
    low = client.get("/reviews?sort=low").text
    assert low.index("Celeste") < low.index("Hades")
    assert 'href="/reviews">Reviews</a>' in client.get("/ratings").text  # reached from My ratings


def test_every_page_has_the_theme_button_and_applies_the_saved_choice_early(client):
    html = client.get("/").text
    assert 'class="theme-toggle"' in html and "/static/theme.js" in html
    assert html.index('localStorage.getItem("shelflife-theme")') < html.index("/static/style.css")
