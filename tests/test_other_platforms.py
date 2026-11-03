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
    assert is_played(entry, False)  # 42.5 hours
    assert otherplatforms.where_you_have_it(game) == "on PlayStation"


def test_a_game_you_already_have_cant_be_added_again(session):
    add_game(session, 1, "Hades", minutes=600)
    add_game(session, 2, "Family Game", owned=False, family=True)
    otherplatforms.add_game(session, 2, "x", "Xbox")
    for app_id, where in ((1, "on Steam"), (2, "through Family Sharing, played on Xbox")):
        with pytest.raises(ValueError, match=where):
            otherplatforms.add_game(session, app_id, "x", "Mobile")


# ---- a family game you played somewhere else ----


def test_a_family_game_can_be_marked_as_played_elsewhere(session):
    add_game(session, 2, "Family Game", owned=False, family=True)
    otherplatforms.add_game(session, 2, "ignored", "PlayStation", 20)
    game, entry = session.get(Game, 2), session.get(LibraryEntry, 2)
    assert game.family_shared and entry.platform == "PlayStation" and entry.playtime_total_min == 1200
    assert is_played(entry, True)
    assert otherplatforms.where_you_have_it(game) == "through Family Sharing, played on PlayStation"


def test_unmarking_a_family_game_keeps_it_in_the_library(session):
    add_game(session, 2, "Family Game", owned=False, family=True)
    otherplatforms.add_game(session, 2, "x", "Xbox", 5)
    otherplatforms.set_platform(session, 2, "")  # "Steam, through Family Sharing" after all
    assert session.get(LibraryEntry, 2).platform is None
    otherplatforms.add_game(session, 2, "x", "Xbox")
    otherplatforms.remove_game(session, 2)
    assert session.get(LibraryEntry, 2) is not None and session.get(LibraryEntry, 2).platform is None


def test_a_family_game_played_elsewhere_drops_its_steam_achievements_and_counts_as_family(session):
    add_game(session, 2, "Family Game", owned=False, family=True)
    assert [e.steam_app_id for e in achievements_due(session)] == [2]
    otherplatforms.add_game(session, 2, "x", "Xbox", 5)
    assert achievements_due(session) == []  # Steam's achievements aren't your Xbox play
    stats = compute_stats(session)
    assert (stats.family, stats.other) == (1, 0)
    assert (what_is_due(session, with_due=False)["family"], what_is_due(session, with_due=False)["other"]) == (1, 0)


def test_steam_achievements_and_dates_dont_count_for_play_elsewhere(session):
    from shelflife.play import PlayScale
    from shelflife.score import engagement_score

    # A family game with 2 of 40 Steam achievements, last unlocked long ago, and 30 hours you played on Xbox.
    add_game(session, 2, "Family Game", owned=False, family=True, achievements=(2, 40), rarest=50.0, unlock_times=[1_000_000])
    entry = session.get(LibraryEntry, 2)
    entry.playtime_total_min = 1800
    session.commit()
    scale = PlayScale([entry])
    assert set(scale.score(entry).parts) == {"hours", "completion", "rarity"}
    otherplatforms.set_platform(session, 2, "Xbox")
    played = scale.score(entry)
    assert set(played.parts) == {"hours"} and played.recency == 0.0
    breakdown = engagement_score(entry)
    assert (breakdown.achievements, breakdown.rare, breakdown.recency) == (0.0, 0.0, 0.0)


def test_family_import_keeps_games_played_elsewhere(session):
    from shelflife.family import FamilyApp, import_family

    add_game(session, 2, "Played elsewhere", owned=False, family=True)
    add_game(session, 3, "Only family", owned=False, family=True)
    otherplatforms.add_game(session, 2, "x", "PlayStation", 8)
    otherplatforms.add_game(session, 4, "PS game", "PlayStation")
    import_family(session, [FamilyApp(4, ("765",), 0, "PS game")], {})  # 2 and 3 no longer shared; 4 now is
    assert session.get(LibraryEntry, 3) is None
    kept = session.get(Game, 2)
    assert kept.library_entry.platform == "PlayStation" and not kept.family_shared  # now a PlayStation game
    assert session.get(Game, 4).family_shared and session.get(LibraryEntry, 4).platform == "PlayStation"


def test_the_add_page_offers_to_mark_a_family_game(client, session, monkeypatch):
    add_game(session, 2, "Family Game", owned=False, family=True)
    monkeypatch.setattr(store, "find_games", lambda term: [store.Found(2, "Family Game", 2020)])
    monkeypatch.setattr(web_app, "enrich_one", lambda game: None)  # no store details in tests
    page = client.get("/library/add?q=family").text
    assert "In your family library. Played it somewhere else?" in page and "Mark as played there" in page
    post(client, "/library/add", app_id="2", name="Family Game", platform="Nintendo Switch", hours="3")
    library = client.get("/library?source=family").text
    assert "Family · Nintendo Switch" in library
    page = client.get("/game/2").text
    assert "Steam, through Family Sharing" in page and "Remove from library" not in page
    assert "achievements</span>" not in page


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
    otherplatforms.add_game(session, pick, "x", "Nintendo Switch", 12)
    after = score_all(session, ratings)
    assert pick not in after.backlog | after.discover | after.family


def test_one_you_havent_played_goes_on_the_backlog_until_you_play_rate_or_track_it(session):
    ratings = build_scenario(session)
    pick = next(iter(score_all(session, ratings).discover))
    otherplatforms.add_game(session, pick, "x", "PlayStation")  # no hours
    assert not is_played(session.get(LibraryEntry, pick), False)
    assert pick in score_all(session, ratings).backlog
    diary.set_status(session, pick, "played", None, None)  # played it, hours unknown
    assert pick not in score_all(session, ratings).backlog


def test_steam_achievements_dont_make_a_family_game_played_elsewhere_played(session):
    add_game(session, 2, "Family Game", owned=False, family=True, achievements=(5, 40))
    assert is_played(session.get(LibraryEntry, 2), True)  # family play on Steam
    otherplatforms.set_platform(session, 2, "Xbox")
    assert not is_played(session.get(LibraryEntry, 2), True)  # only your typed hours tell now


def test_stats_and_sync_count_them_apart(session):
    add_game(session, 1, "Owned", minutes=600)
    add_game(session, 2, "Family", owned=False, family=True)
    otherplatforms.add_game(session, 3, "Elsewhere", "Mobile")  # no hours: on the backlog
    otherplatforms.add_game(session, 4, "Played elsewhere", "Mobile", 9)
    stats = compute_stats(session)
    assert (stats.owned, stats.family, stats.other, stats.backlog, stats.played) == (1, 1, 2, 1, 2)
    due = what_is_due(session, with_due=False)
    assert (due["owned"], due["family"], due["other"]) == (1, 1, 2)


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


def test_adding_goes_back_to_the_search_and_reads_the_store_details_after_answering(client, session, monkeypatch):
    fetched = []

    def enrich(game):
        fetched.append(game.steam_app_id)
        game.type, game.details_fetched_at = "game", otherplatforms._now()

    monkeypatch.setattr(web_app, "enrich_one", enrich)
    response = post(client, "/library/add", app_id="1593500", name="God of War", platform="PlayStation", hours="40,5",
                    next="/library/add?q=god+of+war")
    assert response.headers["location"] == "/library/add?q=god+of+war"  # on to the next game
    assert fetched == [1593500]  # in the background, once the page had answered
    session.expire_all()
    assert session.get(Game, 1593500).details_fetched_at is not None
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
    assert session.get(Game, 7).details_fetched_at is None


def test_a_bad_platform_is_refused(client):
    assert post(client, "/library/add", app_id="7", name="Game", platform="").status_code == 400


def test_changing_the_platform_and_removing_from_the_game_page(client, session):
    otherplatforms.add_game(session, 7, "Game", "Xbox")
    post(client, "/game/7/platform", platform="Mobile")
    assert session.get(LibraryEntry, 7).platform == "Mobile"
    post(client, "/game/7/platform", remove="1")
    session.expire_all()
    assert session.get(LibraryEntry, 7) is None


# ---- wishlisted, but you have it elsewhere ----


def test_a_wishlisted_game_you_have_on_another_platform_is_flagged_everywhere(client, session):
    from datetime import datetime

    from shelflife.models import WishlistItem

    add_game(session, 1, "Loved", tags={"Stealth": 100}, minutes=6000)
    diary.save_rating(session, 1, 5.0)
    add_game(session, 5, "Thief Again", tags={"Stealth": 100}, owned=False, reviews=(900, 100))
    add_game(session, 6, "Only wished", tags={"Stealth": 90}, owned=False, reviews=(900, 100))
    otherplatforms.add_game(session, 5, "x", "PlayStation", 20)
    for app_id in (5, 6):
        session.add(WishlistItem(steam_app_id=app_id, priority=0, added_at=datetime(2026, 1, 1), seen_at=datetime(2026, 1, 1)))
    session.commit()
    assert otherplatforms.elsewhere(session.get(Game, 5)) == "PlayStation" and otherplatforms.elsewhere(session.get(Game, 6)) is None

    wishlist = client.get("/wishlist").text
    assert "You already have 1 of these on other platforms" in wishlist and "Thief Again</a> (PlayStation)" in wishlist
    assert "⚠ Have it on PlayStation" in wishlist and wishlist.count("⚠ Have it on") == 1  # only that card
    assert "already have it on PlayStation" in client.get("/game/5").text
    assert "already have it on" not in client.get("/game/6").text
    library = client.get("/library").text
    assert library.count("⚠ Wishlisted") == 1
