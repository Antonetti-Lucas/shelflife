"""Talking to Steam and its helpers (with fake responses, no network), and the sync."""

from datetime import datetime, timezone

import pytest
import requests

from shelflife import appnames, reviews, store, sync
from shelflife import achievements as ach
from shelflife.models import Game, LibraryEntry, PlaytimeSnapshot
from shelflife.steam import OwnedGame, SteamError
from shelflife.throttle import RateLimited
from tests.conftest import FakeResponse
from tests.factories import add_game

# ---- achievements ----

PLAYER = {
    "playerstats": {
        "achievements": [
            {"apiname": "A", "achieved": 1, "unlocktime": 1_600_000_200},
            {"apiname": "B", "achieved": 1, "unlocktime": 1_600_000_100},
            {"apiname": "C", "achieved": 0, "unlocktime": 0},
            {"apiname": "D", "achieved": 1, "unlocktime": 0},  # unlocked, but with no date
        ]
    }
}
GLOBAL = {"achievementpercentages": {"achievements": [{"name": "A", "percent": "42.5"}, {"name": "B", "percent": 3.1}, {"name": "C", "percent": 1.0}]}}


def fake_steam(monkeypatch, player_status=200, player_json=PLAYER):
    def fake_get(url, params, timeout):
        if url == ach.PLAYER_URL:
            return FakeResponse(player_status, player_json)
        return FakeResponse(200, GLOBAL)

    monkeypatch.setattr(ach, "_get", fake_get)


def test_achievement_progress_rarest_and_dates_are_read(monkeypatch):
    fake_steam(monkeypatch)
    stats = ach.fetch_achievements("key", "id", 1)
    assert (stats.total, stats.unlocked) == (4, 3)
    assert stats.rarest_unlocked_pct == 3.1  # the rarest of the ones you hold; C is not held
    assert stats.unlock_times == (1_600_000_100, 1_600_000_200)  # sorted; undated unlock left out
    assert stats.first_unlock_at < stats.last_unlock_at


def test_a_game_without_achievements_is_not_an_error(monkeypatch):
    fake_steam(monkeypatch, player_status=400)
    assert ach.fetch_achievements("key", "id", 1) is ach.NO_ACHIEVEMENTS


def test_a_steam_server_error_for_one_game_is_reported_as_unavailable(monkeypatch):
    fake_steam(monkeypatch, player_status=500)
    with pytest.raises(ach.AchievementsUnavailable):
        ach.fetch_achievements("key", "id", 1)


def test_a_private_profile_is_a_hard_error_not_a_skip(monkeypatch):
    fake_steam(monkeypatch, player_status=403)
    with pytest.raises(SteamError) as caught:
        ach.fetch_achievements("key", "id", 1)
    assert not isinstance(caught.value, ach.AchievementsUnavailable)


# ---- library sync ----


def owned(app_id, minutes=0, recent=0, last_played=0, name="Game"):
    return OwnedGame(app_id, name, minutes, recent, last_played)


def test_syncing_creates_games_entries_and_a_first_snapshot(session):
    sync.upsert_library(session, [owned(1, minutes=120, recent=30, last_played=1_700_000_000, name="Hades")])
    assert session.get(Game, 1).name == "Hades"
    entry = session.get(LibraryEntry, 1)
    assert (entry.playtime_total_min, entry.playtime_recent_min) == (120, 30)
    assert entry.last_played == datetime(2023, 11, 14, 22, 13, 20)
    assert session.query(PlaytimeSnapshot).count() == 1


def test_a_snapshot_is_added_only_when_playtime_changed(session):
    sync.upsert_library(session, [owned(1, minutes=120)])
    sync.upsert_library(session, [owned(1, minutes=120)])
    assert session.query(PlaytimeSnapshot).count() == 1
    sync.upsert_library(session, [owned(1, minutes=180)])
    assert [s.playtime_total_min for s in session.query(PlaytimeSnapshot).order_by(PlaytimeSnapshot.id)] == [120, 180]


def test_never_played_games_have_no_last_played_date(session):
    sync.upsert_library(session, [owned(1)])
    assert session.get(LibraryEntry, 1).last_played is None


def test_a_family_game_you_buy_stops_being_a_family_game(session):
    add_game(session, 1, owned=False, family=True)
    sync.upsert_library(session, [owned(1, minutes=300)])
    assert session.get(Game, 1).family_shared is False
    assert session.get(LibraryEntry, 1).playtime_total_min == 300


def test_syncing_keeps_details_that_were_already_fetched(session):
    add_game(session, 1, tags={"Stealth": 10})
    sync.upsert_library(session, [owned(1, minutes=60, name="Renamed")])
    game = session.get(Game, 1)
    assert (game.name, game.tags) == ("Renamed", {"Stealth": 10})


# ---- achievement sync ----


def stats(unlocked=2, times=(100, 200)):
    return ach.AchievementStats(10, unlocked, 5.0, datetime(2020, 1, 1), datetime(2021, 1, 1), tuple(times))


class Calls:
    def __init__(self, behaviour):
        self.ids, self.behaviour = [], behaviour

    def __call__(self, api_key, steam_id, app_id):
        self.ids.append(app_id)
        return self.behaviour(app_id)


def test_new_games_get_their_achievements_and_unlock_times_stored(session, monkeypatch):
    add_game(session, 1, minutes=600)
    calls = Calls(lambda app_id: stats())
    monkeypatch.setattr(sync, "fetch_achievements", calls)
    assert sync.sync_achievements(session, "key", "id") == 1
    entry = session.get(LibraryEntry, 1)
    assert (entry.achievements_unlocked, entry.achievements_total) == (2, 10)
    assert entry.unlock_times == [100, 200]


def test_family_games_are_fetched_even_with_no_playtime(session, monkeypatch):
    add_game(session, 1, owned=False, family=True)
    calls = Calls(lambda app_id: stats())
    monkeypatch.setattr(sync, "fetch_achievements", calls)
    sync.sync_achievements(session, "key", "id")
    assert calls.ids == [1]


def test_unplayed_owned_games_are_not_fetched(session, monkeypatch):
    add_game(session, 1, minutes=0)
    calls = Calls(lambda app_id: stats())
    monkeypatch.setattr(sync, "fetch_achievements", calls)
    sync.sync_achievements(session, "key", "id")
    assert calls.ids == []


def test_games_fetched_before_unlock_times_existed_are_backfilled_once(session, monkeypatch):
    add_game(session, 1, minutes=600, achievements=(5, 10))
    session.get(LibraryEntry, 1).achievements_fetched_at = datetime(2026, 1, 1)
    session.commit()
    calls = Calls(lambda app_id: stats(unlocked=5, times=(100,)))
    monkeypatch.setattr(sync, "fetch_achievements", calls)
    sync.sync_achievements(session, "key", "id")
    sync.sync_achievements(session, "key", "id")  # the second run has nothing left to do
    assert calls.ids == [1]


def test_games_with_no_unlocks_are_not_fetched_again(session, monkeypatch):
    add_game(session, 1, minutes=600)
    calls = Calls(lambda app_id: stats(unlocked=0, times=()))
    monkeypatch.setattr(sync, "fetch_achievements", calls)
    sync.sync_achievements(session, "key", "id")
    sync.sync_achievements(session, "key", "id")
    assert calls.ids == [1]


def test_games_played_in_the_last_two_weeks_are_refreshed(session, monkeypatch):
    add_game(session, 1, minutes=600, recent=30)
    session.get(LibraryEntry, 1).achievements_fetched_at = datetime(2026, 1, 1)
    session.commit()
    calls = Calls(lambda app_id: stats())
    monkeypatch.setattr(sync, "fetch_achievements", calls)
    sync.sync_achievements(session, "key", "id")
    assert calls.ids == [1]


def test_one_game_steam_cannot_answer_for_does_not_stop_the_others(session, monkeypatch):
    add_game(session, 1, minutes=900)
    add_game(session, 2, minutes=600)

    def behaviour(app_id):
        if app_id == 1:
            raise ach.AchievementsUnavailable("broken")
        return stats()

    monkeypatch.setattr(sync, "fetch_achievements", Calls(behaviour))
    assert sync.sync_achievements(session, "key", "id") == 1
    assert session.get(LibraryEntry, 2).achievements_total == 10
    assert session.get(LibraryEntry, 1).achievements_fetched_at is None  # retried next run


def test_repeated_failures_stop_the_run_instead_of_hammering_steam(session, monkeypatch):
    for app_id in range(1, 6):
        add_game(session, app_id, minutes=600)

    def behaviour(app_id):
        raise SteamError("down")

    monkeypatch.setattr(sync, "fetch_achievements", Calls(behaviour))
    with pytest.raises(SteamError):
        sync.sync_achievements(session, "key", "id")


# ---- store, SteamSpy, reviews, app names ----


def fake_requests(monkeypatch, module, handler):
    monkeypatch.setattr(module.requests, "get", lambda url, **kwargs: handler(url, kwargs))


def test_store_details_are_parsed(monkeypatch):
    body = {"7": {"success": True, "data": {"type": "game", "name": "Seven", "short_description": "d", "genres": [{"description": "Action"}], "release_date": {"date": "1 Jan, 2020"}, "header_image": "http://img"}}}
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(200, body))
    details = store.fetch_store_details(7)
    assert (details.name, details.type, details.genres, details.release_date, details.cover_url) == (
        "Seven", "game", ["Action"], "1 Jan, 2020", "http://img")


def test_an_app_without_a_store_page_gives_none(monkeypatch):
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(200, {"7": {"success": False}}))
    assert store.fetch_store_details(7) is None


def test_rate_limiting_stops_the_fetch_with_a_clear_error(monkeypatch):
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(429, {}))
    with pytest.raises(RateLimited):
        store.fetch_store_details(7)
    with pytest.raises(RateLimited):
        store.fetch_spy_details(7)


def test_tags_are_read_from_the_store_page(monkeypatch):
    html = 'x InitAppTagModal( 686060, [{"tagid":1,"name":"Roguelite","count":545,"browseable":true},{"tagid":2,"name":"Cats","count":518,"browseable":true}], [], {} ) y'
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(200, text=html))
    assert store.fetch_store_page_tags(686060) == {"Roguelite": 545, "Cats": 518}


def test_a_store_page_without_tags_gives_an_empty_dict(monkeypatch):
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(200, text="<html></html>"))
    assert store.fetch_store_page_tags(1) == {}


def test_steamspy_returns_a_list_instead_of_a_dict_when_there_are_no_tags(monkeypatch):
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(200, {"tags": [], "positive": 10, "negative": 2}))
    details = store.fetch_spy_details(1)
    assert (details.tags, details.positive, details.negative) == ({}, 10, 2)


def test_a_tag_list_from_steamspy_is_parsed(monkeypatch):
    body = {"1": {"appid": 1, "name": "A", "positive": 90, "negative": 10}, "2": {"appid": 2, "name": "B"}}
    fake_requests(monkeypatch, store, lambda url, kw: FakeResponse(200, body))
    listings = store.fetch_spy_tag_list("Stealth")
    assert [(g.app_id, g.name, g.positive, g.negative) for g in listings] == [(1, "A", 90, 10), (2, "B", 0, 0)]


def test_review_counts_are_read_from_the_summary(monkeypatch):
    body = {"response": {"query_summary": {"total_positive": 95, "total_negative": 5}}}
    fake_requests(monkeypatch, reviews, lambda url, kw: FakeResponse(200, body))
    counts = reviews.fetch_review_counts(1)
    assert (counts.positive, counts.negative) == (95, 5)


def test_missing_review_summary_gives_none(monkeypatch):
    fake_requests(monkeypatch, reviews, lambda url, kw: FakeResponse(200, {"response": {"query_summary": {}}}))
    assert reviews.fetch_review_counts(1) is None


def test_app_names_are_collected_page_by_page_and_stop_when_done(monkeypatch):
    pages = {
        0: {"response": {"apps": [{"appid": 1, "name": "One"}, {"appid": 2, "name": "Two"}], "have_more_results": True, "last_appid": 2}},
        2: {"response": {"apps": [{"appid": 3, "name": "Three"}, {"appid": 4, "name": "Four"}], "have_more_results": False, "last_appid": 4}},
    }
    seen = []

    def handler(url, kw):
        seen.append(kw["params"]["last_appid"])
        return FakeResponse(200, pages[kw["params"]["last_appid"]])

    fake_requests(monkeypatch, appnames, handler)
    assert appnames.fetch_app_names("key", {2, 4}) == {2: "Two", 4: "Four"}
    assert seen == [0, 2]


def test_app_name_paging_stops_once_past_the_largest_id_needed(monkeypatch):
    seen = []

    def handler(url, kw):
        seen.append(kw["params"]["last_appid"])
        return FakeResponse(200, {"response": {"apps": [{"appid": 1, "name": "One"}, {"appid": 2, "name": "Two"}], "have_more_results": True, "last_appid": 2}})

    fake_requests(monkeypatch, appnames, handler)
    assert appnames.fetch_app_names("key", {1}) == {1: "One"}
    assert seen == [0]  # app 1 was on the first page, and nothing larger was needed


def test_nothing_is_requested_when_no_names_are_needed(monkeypatch):
    fake_requests(monkeypatch, appnames, lambda url, kw: pytest.fail("no request expected"))
    assert appnames.fetch_app_names("key", set()) == {}
