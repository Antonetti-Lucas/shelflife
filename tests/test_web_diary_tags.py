"""The diary tabs, the tag filter and the recent-play sort, driven through the web app."""

import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.web.app import app
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def scenario(session):
    ratings = build_scenario(session)
    for app_id, rating in ratings.items():
        diary.save_rating(session, app_id, rating)
    return ratings


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def names_on(html):
    return re.findall(r'<a href="/game/\d+"[^>]*>([^<]+)</a>', html)


def library_ids(html):
    return re.findall(r'<tr id="g(\d+)"', html)


def tracked_scenario(client):
    post(client, "/game/20/status", status="played", finished_on="2025-01-01")
    post(client, "/game/21/status", status="played", finished_on="2026-03-01")
    post(client, "/game/1/status", status="played", finished_on="")  # undated: last
    post(client, "/game/30/status", status="playing", started_on="2026-05-05")
    post(client, "/game/2/status", status="dropped")
    post(client, "/game/40/favorite", value="1")


# ---- the diary tabs ----


def test_the_diary_shows_one_status_at_a_time_newest_first(client, scenario):
    tracked_scenario(client)
    assert names_on(client.get("/diary?show=played").text) == ["Backlog Sports", "Backlog Stealth", "Stealth Hero"]
    assert names_on(client.get("/diary?show=playing").text) == ["Family Stealth"]
    assert names_on(client.get("/diary?show=dropped").text) == ["Sports Pro"]
    assert names_on(client.get("/diary?show=favorites").text) == ["Pool Stealth Good"]


def test_the_diary_tabs_show_how_many_games_each_has(client, scenario):
    tracked_scenario(client)
    html = client.get("/diary").text
    assert "Played <small>3</small>" in html and "Playing <small>1</small>" in html
    assert "Dropped <small>1</small>" in html and "Favorites <small>1</small>" in html


def test_the_diary_opens_on_the_first_tab_that_has_games(client, scenario):
    post(client, "/game/20/status", status="dropped")
    assert names_on(client.get("/diary").text) == ["Backlog Stealth"]  # nothing playing or played
    assert names_on(client.get("/diary?show=nonsense").text) == ["Backlog Stealth"]


def test_the_diary_says_how_to_start_when_a_tab_is_empty(client, scenario):
    html = client.get("/diary").text
    assert "Nothing yet" in html and names_on(html) == []
    assert "Nothing dropped" in client.get("/diary?show=dropped").text
    assert "No favorites yet" in client.get("/diary?show=favorites").text


def test_diary_reviews_are_escaped_and_dates_shown(client, scenario):
    post(client, "/game/20/status", status="played", started_on="2025-01-02", finished_on="2025-02-03")
    post(client, "/game/20/rate", rating="4", review="a <b>bold</b> take")
    html = client.get("/diary?show=played").text
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>bold</b>" not in html
    assert "started 2025-01-02" in html and "finished 2025-02-03" in html
    assert "rated 4" in html


def test_the_diary_is_paged_and_pages_keep_the_tab_and_filters(client, session):
    for i in range(100, 130):
        add_game(session, i, f"Game {i}", tags={"Stealth": 100})
        diary.set_status(session, i, "played", None, date(2025, 1, 1) + timedelta(days=i))
    first = names_on(client.get("/diary?show=played").text)
    assert len(first) == 12 and first[0] == "Game 129"  # newest first
    assert len(names_on(client.get("/diary?show=played&page=3").text)) == 6
    assert len(names_on(client.get("/diary?show=played&page=99").text)) == 6  # clamped
    assert len(names_on(client.get("/diary?show=played&page=-4").text)) == 12
    html = client.get("/diary?show=played&q=game&tag=stealth&page=2").text
    assert 'href="/diary?show=played&amp;q=game&amp;tag=stealth&amp;page=3"' in html  # Next
    assert 'href="/diary?show=played&amp;q=game&amp;tag=stealth&amp;page=1"' in html  # Previous
    assert "show=dropped&amp;q=game&amp;tag=stealth&amp;page=1" in html  # another tab starts at page 1


def test_the_diary_can_be_searched_and_filtered_by_tag(client, scenario):
    tracked_scenario(client)
    assert names_on(client.get("/diary?show=played&q=stealth").text) == ["Backlog Stealth", "Stealth Hero"]
    assert names_on(client.get("/diary?show=played&tag=action").text) == ["Backlog Stealth", "Stealth Hero"]
    assert names_on(client.get("/diary?show=played&tag=Stealth,Sports").text) == []  # a game needs every tag
    html = client.get("/diary?show=played&tag=Nonexistent").text
    assert "No game here has the tag Nonexistent" in html and "No games match" in html


# ---- tags and recent play in the library and explore ----


def test_the_library_can_be_filtered_by_tag_ignoring_case(client, scenario):
    assert set(library_ids(client.get("/library?tag=sports").text)) == {"2", "21"}
    assert set(library_ids(client.get("/library?tag=Stealth,action").text)) == {"1", "20"}  # both tags
    assert library_ids(client.get("/library?tag=Stealth,Sports").text) == []


def test_an_unknown_tag_is_called_out_in_the_library_and_explore(client, scenario):
    assert "No game here has the tag Nonexistent" in client.get("/library?tag=Nonexistent").text
    assert "No game here has the tag Nonexistent" in client.get("/explore?tag=Nonexistent").text
    assert "No game here has the tag" not in client.get("/library?tag=Stealth,Sports").text  # real tags, no overlap


def test_the_library_suggests_the_tags_it_has(client, scenario):
    html = client.get("/library").text
    assert '<option value="Stealth">' in html and '<option value="Sports">' in html
    assert '<option value="Cats">' not in html


def test_the_library_pager_keeps_the_tag_filter(client, session):
    for i in range(100, 150):
        add_game(session, i, f"Game {i}", tags={"Stealth": 100}, minutes=600)
    html = client.get("/library?tag=Stealth").text
    assert "tag=Stealth" in re.search(r'class="pager".*?</p>', html, re.S).group(0)


def test_a_tag_a_game_barely_has_does_not_count(client, session):
    votes = {f"Tag {i}": 1000 - i for i in range(25)}  # 25 tags; the last 5 fall outside the top 20
    add_game(session, 1, "Many Tags", tags=votes, minutes=600)
    assert library_ids(client.get("/library?tag=Tag+0").text) == ["1"]
    assert library_ids(client.get("/library?tag=Tag+24").text) == []


def test_the_library_sorts_by_most_recently_played_with_undated_games_last(client, session):
    add_game(session, 200, "Old One", minutes=100, last_played=datetime(2024, 1, 1))
    add_game(session, 201, "New One", minutes=100, last_played=datetime(2026, 5, 1, 12))
    add_game(session, 202, "Never One", minutes=100)
    add_game(session, 203, "Family One", owned=False, family=True)
    html = client.get("/library?sort=recent").text
    ids = library_ids(html)
    assert ids[:2] == ["201", "200"] and set(ids[2:]) == {"202", "203"}
    assert "2026-05-01" in html and "2024-01-01" in html
    assert 'value="recent" selected' in html


def test_explore_can_be_filtered_by_tag(client, scenario):
    assert "Pool Stealth Good" in client.get("/explore?tag=Stealth,Action").text
    html = client.get("/explore?tag=Sports").text
    assert "Pool Stealth Good" not in html and "No games match" in html
    assert '<option value="Stealth">' in client.get("/explore").text
