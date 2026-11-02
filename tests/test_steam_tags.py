"""Steam's own tags: read in bulk, the games under a tag, and how Sync and searches use them,
with SteamSpy as the fallback."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from shelflife import discovery, store
from shelflife.discovery import find_games
from shelflife.models import Game
from shelflife.store import Listing, SpyDetails
from shelflife.store import fetch_steam_tag_games as real_tag_games
from shelflife.store import fetch_steam_tags as real_fetch_tags
from shelflife.sync import STEAM_TAGS_SINCE, TAGS_REFRESH_DAYS, enrich_one, sync_tags, tags_due
from shelflife.tagfilter import parse_tags
from tests.conftest import FakeResponse
from tests.factories import add_game

NAMES = {1: "Stealth", 2: "Roguelike", 3: "Puzzle", 6: "Pizza"}


@pytest.fixture
def known_tags(monkeypatch):
    """Steam's tag list, as if already read."""
    monkeypatch.setattr(store, "_tag_names", (time.monotonic(), dict(NAMES)))


def item(app_id, *tag_ids, reviews=None, success=1):
    """A game as Steam's API describes it: its tags (the first with the most weight) and reviews."""
    out = {
        "appid": app_id,
        "success": success,
        "name": f"Game {app_id}",
        "tags": [{"tagid": t, "weight": 1000 - 100 * i} for i, t in enumerate(tag_ids)],
    }
    if reviews:
        count, percent = reviews
        out["reviews"] = {"summary_filtered": {"review_count": count, "percent_positive": percent}}
    return out


@pytest.fixture
def api(monkeypatch):
    """A fake api.steampowered.com: answers queued per method; every request is recorded."""
    asked, answers = [], {}

    def get(url, params=None, headers=None, timeout=None):
        method = url.split("api.steampowered.com/")[1].removesuffix("/v1/")
        asked.append((method, json.loads(params["input_json"]) if "input_json" in params else params))
        queue = answers.get(method) or [{}]
        return FakeResponse(200, {"response": queue.pop(0) if len(queue) > 1 else queue[0]})

    monkeypatch.setattr(store.requests, "get", get)
    get.asked, get.answers = asked, answers
    return get


def bodies(api, method):
    return [body for m, body in api.asked if m == method]


# ---- reading tags ----


def test_games_tags_come_by_name_200_games_a_call_in_your_country(api, known_tags, monkeypatch):
    monkeypatch.setenv("STEAM_COUNTRY", "br")
    api.answers["IStoreBrowseService/GetItems"] = [
        {"store_items": [item(1, 1, 3), item(2, 2)]},
        {"store_items": [item(250, 6)]},
    ]
    assert real_fetch_tags(list(range(1, 251))) == {
        1: {"Stealth": 1000, "Puzzle": 900},
        2: {"Roguelike": 1000},
        250: {"Pizza": 1000},
    }
    calls = bodies(api, "IStoreBrowseService/GetItems")
    assert [len(c["ids"]) for c in calls] == [200, 50]
    assert calls[0]["context"]["country_code"] == "BR" and calls[0]["data_request"]["include_tag_count"] == 20


def test_without_a_country_steam_is_asked_as_the_us(api, known_tags, monkeypatch):
    monkeypatch.delenv("STEAM_COUNTRY", raising=False)
    real_fetch_tags([1])
    assert bodies(api, "IStoreBrowseService/GetItems")[0]["context"]["country_code"] == "US"


def test_games_steam_has_no_tags_for_are_left_out(api, known_tags):
    api.answers["IStoreBrowseService/GetItems"] = [{"store_items": [
        item(2, 1, success=15),  # not sold in your country
        item(99, 1),  # Steam moved game 3 to a new id: not the game asked for
        item(4),  # no tags
    ]}]
    assert real_fetch_tags([2, 3, 4]) == {}


def test_steams_tag_list_is_read_once_and_names_match_ignoring_case(api, monkeypatch):
    monkeypatch.setattr(store, "_tag_names", None)
    api.answers["IStoreService/GetTagList"] = [{"tags": [{"tagid": 1, "name": "Roguelike"}, {"tagid": 2, "name": "Mouse Only"}]}]
    assert store.steam_tag_id("roguelike") == 1
    assert store.steam_tag_id("Rogue-like") == 1  # SteamSpy's old name
    assert store.steam_tag_id("Nope") is None
    assert len([m for m, _ in api.asked if m == "IStoreService/GetTagList"]) == 1


def test_steamspy_tags_are_renamed_as_steam_names_them_and_retired_ones_dropped(known_tags):
    spy = {"Rogue-like": 50, "stealth": 40, "Mature": 30, "Roguelike": 60}
    assert store.steam_names(spy) == {"Roguelike": 60, "Stealth": 40}


def test_when_steams_tag_list_cant_be_read_only_old_names_are_renamed(monkeypatch):
    monkeypatch.setattr(store, "_tag_names", None)

    def down(timeout=30):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(store, "steam_tag_names", down)
    assert store.steam_names({"Rogue-lite": 5, "Mature": 3}) == {"Roguelite": 5, "Mature": 3}


# ---- the games under a tag ----


def test_the_games_under_a_tag_come_with_review_counts_and_tags(api, known_tags):
    api.answers["IStoreQueryService/Query"] = [{
        "store_items": [item(1, 1, 3, reviews=(1000, 90)), item(2, 1, reviews=(10, 50)), item(3, 1)],
        "metadata": {"total_matching_records": 3},
    }]
    games = real_tag_games(1, min_reviews=300)
    assert games == [Listing(1, "Game 1", 900, 100), Listing(2, "Game 2", 5, 5), Listing(3, "Game 3", 0, 0)]
    assert games[0].tags == {"Stealth": 1000, "Puzzle": 900}
    query = bodies(api, "IStoreQueryService/Query")[0]["query"]
    assert query["filters"]["tagids_must_match"] == [{"tagids": [1]}] and query["filters"]["released_only"]
    assert query["sort"] == store.TAG_QUERY_SORT


def test_pages_are_read_until_one_brings_few_well_reviewed_games(api, known_tags, monkeypatch):
    monkeypatch.setattr(store, "TAG_QUERY_PAGE", 2)
    monkeypatch.setattr(store, "TAG_QUERY_ENOUGH", 1)
    many = {"metadata": {"total_matching_records": 100}}
    api.answers["IStoreQueryService/Query"] = [
        {**many, "store_items": [item(1, reviews=(500, 90)), item(2, reviews=(500, 90))]},
        {**many, "store_items": [item(3, reviews=(500, 90)), item(4, reviews=(10, 90))]},
        {**many, "store_items": [item(5, reviews=(10, 90)), item(6, reviews=(10, 90))]},  # none with 300+
        {**many, "store_items": [item(7, reviews=(500, 90)), item(8, reviews=(500, 90))]},
    ]
    games = real_tag_games(1, min_reviews=300)
    assert [g.app_id for g in games] == [1, 2, 3, 4, 5, 6]
    assert [b["query"]["start"] for b in bodies(api, "IStoreQueryService/Query")] == [0, 2, 4]


def test_a_short_page_is_the_last(api, known_tags):
    api.answers["IStoreQueryService/Query"] = [{"store_items": [item(1, reviews=(500, 90))], "metadata": {"total_matching_records": 1}}]
    assert len(real_tag_games(1, min_reviews=300)) == 1
    assert len(bodies(api, "IStoreQueryService/Query")) == 1


# ---- searches ----


@pytest.fixture
def no_store_step(monkeypatch):
    monkeypatch.setattr(discovery, "enrich_one", lambda game: setattr(game, "type", "game"))


def test_a_search_uses_steams_list_and_its_tags_without_asking_steamspy(session, monkeypatch, known_tags, no_store_step):
    off_topic = {"Pizza": 1000, **{f"Other {i}": 900 - i for i in range(12)}, "Stealth": 10}
    listings = [
        Listing(1, "Thief", 9000, 100, {"Stealth": 1000, "Puzzle": 900}),
        Listing(2, "Pizza Game", 8000, 100, off_topic),  # Stealth is its 14th tag
    ]
    monkeypatch.setattr(store, "fetch_steam_tag_games", lambda tag_id, min_reviews=0, timeout=90: listings if tag_id == 1 else [])

    def refuse(*args, **kwargs):
        pytest.fail("SteamSpy was asked")

    monkeypatch.setattr(store, "fetch_spy_tag_games", refuse)
    monkeypatch.setattr(store, "fetch_spy_details", refuse)
    result = find_games(session, ["stealth"], 5)
    assert (result.added, result.skipped, result.checked) == (1, 1, 2)
    kept = session.get(Game, 1)
    assert kept.tags == {"Stealth": 1000, "Puzzle": 900} and kept.tags_fetched_at is not None and kept.type == "game"
    assert session.get(Game, 2) is None  # not remembered: Steam's list brings its tags again


def test_when_steam_cant_be_reached_a_search_uses_steamspy(session, monkeypatch, known_tags, no_store_step):
    def down(tag_id, min_reviews=0, timeout=90):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(store, "fetch_steam_tag_games", down)
    monkeypatch.setattr(store, "fetch_spy_tag_games", lambda tag: [Listing(1, "Thief", 9000, 100)])
    monkeypatch.setattr(store, "fetch_spy_details", lambda app_id: SpyDetails({"Stealth": 50, "Rogue-like": 40, "Mature": 30}, 9000, 100))
    steps = []
    result = find_games(session, ["Stealth"], 5, on_progress=lambda *a: steps.append(a[3]))
    assert result.added == 1 and "Looking up “Stealth” on SteamSpy" in steps
    assert session.get(Game, 1).tags == {"Stealth": 50, "Roguelike": 40}  # in Steam's names


def test_a_tag_steam_doesnt_know_is_looked_up_on_steamspy(session, monkeypatch, known_tags, no_store_step):
    monkeypatch.setattr(store, "fetch_steam_tag_games", lambda *a, **k: pytest.fail("Steam has no id for it"))
    asked = []
    monkeypatch.setattr(store, "fetch_spy_tag_games", lambda tag: asked.append(tag) or [])
    result = find_games(session, ["Beat 'em up"], 5)
    assert asked == ["Beat 'em up"] and result.empty_tags == ["Beat 'em up"]


# ---- Sync ----


def test_a_sync_reads_the_due_games_tags_from_steam(session, monkeypatch, known_tags):
    add_game(session, 1, tags={"Rogue-like": 50})  # SteamSpy's, from before
    add_game(session, 2, tags={"Rogue-like": 50, "Mature": 9})  # Steam has none for it
    add_game(session, 3)  # no tags at all
    add_game(session, 4, type="dlc")  # not a game, no tags
    asked = []
    monkeypatch.setattr(store, "fetch_steam_tags", lambda ids, timeout=60: asked.extend(ids) or {1: {"Roguelike": 700, "Puzzle": 600}})
    spied = []
    monkeypatch.setattr(store, "fetch_spy_details", lambda app_id, timeout=15: spied.append(app_id) or SpyDetails({"stealth": 20}, 90, 10))
    assert sync_tags(session) == 4
    assert sorted(asked) == [1, 2, 3, 4] and spied == [3]
    tags = {i: session.get(Game, i).tags for i in (1, 2, 3, 4)}
    assert tags == {1: {"Roguelike": 700, "Puzzle": 600}, 2: {"Roguelike": 50}, 3: {"Stealth": 20}, 4: {}}
    assert tags_due(session) == []


def test_tags_are_read_once_more_after_the_switch_to_steam_and_then_monthly(session):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    read = {
        1: None,
        2: STEAM_TAGS_SINCE - timedelta(hours=1),  # SteamSpy's, from before the switch
        3: now - timedelta(days=TAGS_REFRESH_DAYS + 1),
        4: now - timedelta(minutes=5),
    }
    for app_id, when in read.items():
        add_game(session, app_id)
        session.get(Game, app_id).tags_fetched_at = when
    session.commit()
    assert sorted(g.steam_app_id for g in tags_due(session)) == [1, 2, 3]
    assert len(tags_due(session, everything=True)) == 4


def test_store_details_bring_steams_tags_only_for_a_game_without_any(session, monkeypatch):
    add_game(session, 1)
    add_game(session, 2, tags={"Puzzle": 5})
    monkeypatch.setattr(store, "fetch_store_details", lambda app_id, timeout=15: store.StoreDetails(type="game", short_description="d"))
    asked = []
    monkeypatch.setattr(store, "fetch_steam_tags", lambda ids, timeout=60: asked.extend(ids) or {1: {"Stealth": 9}})
    enrich_one(session.get(Game, 1))
    enrich_one(session.get(Game, 2))
    assert asked == [1]
    assert session.get(Game, 1).tags == {"Stealth": 9} and session.get(Game, 2).tags == {"Puzzle": 5}


def test_a_filter_written_with_steamspys_old_names_still_finds_the_games():
    assert parse_tags("Rogue-like, -Rogue-lite, Puzzle-Platformer") == ["Roguelike", "-Roguelite", "Puzzle Platformer"]
