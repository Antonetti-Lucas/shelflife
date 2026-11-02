"""Leaving tags out: "-Horror" in a tag box keeps out the games that have it, in the lists and in
the search for new games."""

import pytest
from fastapi.testclient import TestClient

from shelflife import discovery, store
from shelflife.discovery import find_games
from shelflife.store import SpyDetails, Listing
from shelflife.tagfilter import TagIndex, matches, parse_tags, split_excluded
from shelflife.web import app as web_app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}
STEALTH = {"Stealth": 100, "Action": 60}
STEALTH_HORROR = {"Stealth": 100, "Horror": 80}
HORROR = {"Horror": 100, "Survival": 50}


def test_a_minus_marks_a_tag_to_leave_out():
    assert split_excluded(parse_tags("Stealth, -Horror, - Gore,  ")) == (["Stealth"], ["Horror", "Gore"])


@pytest.mark.parametrize(
    "wanted, match, expected",
    [
        (["-Horror"], "all", [True, False, False]),
        (["Stealth", "-Horror"], "all", [True, False, False]),
        (["Stealth", "-Horror"], "any", [True, False, False]),
        (["Stealth", "Horror"], "any", [True, True, True]),
        (["-horror"], "all", [True, False, False]),  # case doesn't matter
    ],
)
def test_a_left_out_tag_keeps_out_every_game_that_has_it(wanted, match, expected):
    assert [matches(t, wanted, match) for t in (STEALTH, STEALTH_HORROR, HORROR)] == expected


def test_a_left_out_tag_no_game_has_is_reported():
    assert TagIndex([STEALTH, HORROR]).unknown(["-Cozy", "Stealth"]) == ["Cozy"]


@pytest.fixture
def client():
    with TestClient(web_app.app) as c:
        yield c


def test_the_library_leaves_out_games_with_the_tag(client, session):
    add_game(session, 1, "Quiet Thief", tags=STEALTH, minutes=600)
    add_game(session, 2, "Scary Thief", tags=STEALTH_HORROR, minutes=600)
    html = client.get("/library?tag=Stealth,-Horror").text
    assert "Quiet Thief" in html and "Scary Thief" not in html


def test_the_search_for_new_games_sets_aside_games_with_a_left_out_tag(session, monkeypatch):
    listings = [Listing(10, "Quiet Thief", 9000, 100), Listing(11, "Scary Thief", 9000, 100)]
    tags = {10: STEALTH, 11: STEALTH_HORROR}
    monkeypatch.setattr(store, "fetch_spy_tag_games", lambda tag: listings)
    monkeypatch.setattr(store, "fetch_spy_details", lambda app_id: SpyDetails(tags[app_id], 9000, 100))
    monkeypatch.setattr(discovery, "enrich_one", lambda game, spy=None: setattr(game, "type", "game"))
    result = find_games(session, ["Stealth"], count=5, exclude=["Horror"])
    assert result.added == 1 and result.skipped == 1


def test_the_find_button_searches_the_tags_and_passes_on_the_ones_left_out(client, session, monkeypatch):
    add_game(session, 1, "Anything", tags={"Stealth": 100, "Horror": 50}, minutes=600)
    calls = []
    monkeypatch.setattr(web_app.discover_job, "start", lambda tags, count, min_reviews, exclude=None: calls.append((tags, exclude)))
    client.post("/explore/find", data={"tags": "stealth, -horror"}, headers=SAME_SITE, follow_redirects=False)
    assert calls == [(["Stealth"], ["Horror"])]  # spelled as the games spell them
