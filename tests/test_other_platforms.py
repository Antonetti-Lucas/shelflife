"""Games you played on other platforms: found on Steam, added by hand, hours typed in."""

import json

import pytest
import requests
from fastapi.testclient import TestClient

from shelflife import diary, otherplatforms, store
from shelflife.hours import set_hours
from shelflife.models import Game, LibraryEntry
from shelflife.play import is_played
from shelflife.recommend import score_all
from shelflife.stats import compute_stats
from shelflife.steam import OwnedGame
from shelflife.sync import achievements_due, upsert_library
from shelflife.sync_job import what_is_due
from shelflife.web import app as web_app
from tests.conftest import FakeResponse
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(web_app.app) as c:
        yield c


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


# ---- adding, changing, removing ----


def test_a_game_from_another_platform_joins_the_library_with_your_hours(session):
    game = otherplatforms.add_game(session, 1593500, "  God of   War ", "PlayStation", 42.5)
    entry = session.get(LibraryEntry, 1593500)
    assert game.name == "God of War" and entry.platform == "PlayStation" and entry.playtime_total_min == 2550
    assert is_played(entry, False)  # added because you played it, hours or not
    assert otherplatforms.where_you_have_it(game) == "on PlayStation"


def test_a_game_you_already_have_cant_be_added_again(session):
    add_game(session, 1, "Hades", minutes=600)
    add_game(session, 2, "Family Game", owned=False, family=True)
    for app_id, where in ((1, "on Steam"), (2, "through Family Sharing")):
        with pytest.raises(ValueError, match=where):
            otherplatforms.add_game(session, app_id, "x", "Xbox")


def test_a_game_from_explore_or_set_aside_by_a_search_becomes_yours(session):
    add_game(session, 1, "In Explore", owned=False)
    add_game(session, 2, "Set aside", owned=False, type="off-topic")
    otherplatforms.add_game(session, 1, "ignored", "Nintendo Switch")
    otherplatforms.add_game(session, 2, "ignored", "Nintendo Switch")
    assert session.get(Game, 1).name == "In Explore"  # the known name is kept
    assert session.get(Game, 2).type is None  # looked up properly at the next fetch


@pytest.mark.parametrize("platform, hours", [("Dreamcast", None), ("Xbox", -1), ("Xbox", 100_001)])
def test_a_platform_must_be_one_of_the_list_and_hours_sensible(session, platform, hours):
    with pytest.raises(ValueError):
        otherplatforms.add_game(session, 1, "Game", platform, hours)


def test_hours_platform_and_removal_can_be_changed_later(session):
    otherplatforms.add_game(session, 1, "Game", "Xbox")
    set_hours(session, 1, 12)
    otherplatforms.set_platform(session, 1, "Another PC store")
    entry = session.get(LibraryEntry, 1)
    assert (entry.playtime_total_min, entry.platform) == (720, "Another PC store")
    diary.save_rating(session, 1, 4.0)
    otherplatforms.remove_game(session, 1)
    assert session.get(LibraryEntry, 1) is None and session.get(Game, 1) is not None
    assert diary.load_ratings(session) == {1: 4.0}  # your verdict stays


def test_only_games_added_from_other_platforms_can_be_removed_this_way(session):
    add_game(session, 1, "Steam game", minutes=60)
    with pytest.raises(ValueError):
        otherplatforms.remove_game(session, 1)


# ---- Sync, recommendations and stats ----


def test_buying_it_on_steam_makes_it_a_steam_game(session):
    otherplatforms.add_game(session, 1, "Game", "PlayStation", 30)
    upsert_library(session, [OwnedGame(1, "Game", 90, 0, 0)])
    entry = session.get(LibraryEntry, 1)
    assert entry.platform is None and entry.playtime_total_min == 90  # Steam's hours now


def test_steam_achievements_are_never_asked_for_other_platforms(session):
    otherplatforms.add_game(session, 1, "Game", "Xbox", 30)
    add_game(session, 2, "Steam game", minutes=600)
    assert [e.steam_app_id for e in achievements_due(session)] == [2]


def test_a_played_game_from_another_platform_is_never_recommended_back(session):
    ratings = build_scenario(session)
    scores = score_all(session, ratings)
    pick = next(iter(scores.discover))  # a game you don't have, recommended
    otherplatforms.add_game(session, pick, "x", "Nintendo Switch")
    after = score_all(session, ratings)
    assert pick not in after.backlog | after.discover | after.family


def test_stats_and_sync_count_them_apart(session):
    add_game(session, 1, "Owned", minutes=600)
    add_game(session, 2, "Family", owned=False, family=True)
    otherplatforms.add_game(session, 3, "Elsewhere", "Mobile")
    stats = compute_stats(session)
    assert (stats.owned, stats.family, stats.other, stats.backlog) == (1, 1, 1, 0)
    due = what_is_due(session, with_due=False)
    assert (due["owned"], due["family"], due["other"]) == (1, 1, 1)


# ---- finding the game on Steam ----


@pytest.fixture
def steam_api(monkeypatch):
    asked = []
    answers = {}

    def get(url, params=None, headers=None, timeout=None):
        method = url.split("api.steampowered.com/")[1].removesuffix("/v1/")
        asked.append((method, json.loads(params["input_json"])))
        return FakeResponse(200, {"response": answers.get(method, {})})

    monkeypatch.setattr(store.requests, "get", get)
    get.asked, get.answers = asked, answers
    return get


def item(app_id, name, kind=0, released=1600300800):
    return {"appid": app_id, "name": name, "type": kind, "success": 1, "release": {"steam_release_date": released}}


def test_a_name_is_searched_with_steams_suggestions_games_only(steam_api):
    steam_api.answers["IStoreQueryService/SearchSuggestions"] = {"store_items": [item(1145360, "Hades"), item(9, "Hades OST", kind=4)]}
    assert store.find_games("hades") == [store.Found(1145360, "Hades", 2020)]
    assert steam_api.asked[0][1]["search_term"] == "hades"


@pytest.mark.parametrize("typed", ["https://store.steampowered.com/app/1145360/Hades/", "1145360", "steamdb.info/app/1145360/"])
def test_a_store_link_or_app_id_finds_that_game(steam_api, typed):
    steam_api.answers["IStoreBrowseService/GetItems"] = {"store_items": [item(1145360, "Hades")]}
    assert [f.app_id for f in store.find_games(typed)] == [1145360]
    assert steam_api.asked[0][0] == "IStoreBrowseService/GetItems"


def test_a_number_that_is_no_games_id_is_searched_as_a_name(steam_api):
    steam_api.answers["IStoreQueryService/SearchSuggestions"] = {"store_items": [item(5, "1942")]}
    assert [f.name for f in store.find_games("1942")] == ["1942"]


# ---- the pages ----


def test_the_add_page_searches_steam_and_marks_games_you_already_have(client, session, monkeypatch):
    add_game(session, 1145360, "Hades", minutes=600)
    found = [store.Found(1145360, "Hades", 2020), store.Found(1145350, "Hades II", 2025)]
    monkeypatch.setattr(store, "find_games", lambda term: found)
    page = client.get("/library/add?q=hades").text
    assert "Already in your library (on Steam)" in page
    assert 'name="app_id" value="1145350"' in page and 'name="app_id" value="1145360"' not in page
    assert "PlayStation" in page and "Hades II <small>2025</small>" in page


def test_the_add_page_says_when_steam_cant_be_reached(client, monkeypatch):
    def down(term):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(store, "find_games", down)
    assert "reach Steam to search" in client.get("/library/add?q=hades").text


def test_adding_reads_the_store_details_now_and_opens_the_game(client, session, monkeypatch):
    fetched = []

    def enrich(game):
        fetched.append(game.steam_app_id)
        game.type, game.details_fetched_at = "game", otherplatforms._now()

    monkeypatch.setattr(web_app, "enrich_one", enrich)
    response = post(client, "/library/add", app_id="1593500", name="God of War", platform="PlayStation", hours="40,5")
    assert response.headers["location"] == "/game/1593500" and fetched == [1593500]
    assert session.get(LibraryEntry, 1593500).playtime_total_min == 2430
    page = client.get("/game/1593500").text
    assert "Remove from library" in page and 'value="PlayStation" selected' in page and "Save hours" in page
    library = client.get("/library?source=other").text
    assert "God of War" in library and "<td>PlayStation</td>" in library
    assert "God of War" not in client.get("/library?source=owned").text


def test_adding_still_works_when_steam_is_down(client, session, monkeypatch):
    def down(game):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(web_app, "enrich_one", down)
    assert post(client, "/library/add", app_id="7", name="Game", platform="Xbox").status_code == 303
    assert session.get(LibraryEntry, 7).platform == "Xbox"  # its details come with the next sync


def test_a_bad_platform_is_refused(client):
    assert post(client, "/library/add", app_id="7", name="Game", platform="").status_code == 400


def test_changing_the_platform_and_removing_from_the_game_page(client, session):
    otherplatforms.add_game(session, 7, "Game", "Xbox")
    post(client, "/game/7/platform", platform="Mobile")
    assert session.get(LibraryEntry, 7).platform == "Mobile"
    post(client, "/game/7/platform", remove="1")
    session.expire_all()
    assert session.get(LibraryEntry, 7) is None
