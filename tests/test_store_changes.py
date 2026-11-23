"""Steam's store change list: a game that changed after we read it is read again soon, a price change
number we have not read makes its price due, and what Steam answers is only used for the games we have.
Steam is faked: no network."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from shelflife import sync
from shelflife.models import Game
from shelflife.settings import get_changes_checked, set_changes_checked
from shelflife.steam import AppChange, SteamError, fetch_changed_apps
from shelflife.sync import (
    CHANGED_BATCH,
    CHANGES_FIRST_DAYS,
    CHANGES_OVERLAP_HOURS,
    details_due,
    prices_due,
    record_store_changes,
    sync_store_changes,
    tags_due,
)
from shelflife.sync_job import STEPS, run_sync, summarize_sync, SyncResult
from shelflife.throttle import RateLimited
from tests.factories import add_game
from tests.test_sync_button import owned, steam  # noqa: F401 (the fake Steam fixture)

HOUR = 3600


def unix(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def read(session, app_id, hours_ago=1000):
    """The game's details and tags were read this many hours ago."""
    game = session.get(Game, app_id)
    game.details_fetched_at = game.tags_fetched_at = NOW - timedelta(hours=hours_ago)
    game.type = "game"
    game.tags = game.tags or {"Action": 100}
    session.commit()
    return game


# ---- the fetch ----


class FakeFeed:
    """Pages of Steam's GetAppList: `pages` is a list of (apps, have_more)."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, url, params, timeout):
        self.asked.append(dict(params))
        apps, more = self.pages[len(self.asked) - 1]
        out = {"apps": apps}
        if more:
            out |= {"have_more_results": True, "last_appid": apps[-1]["appid"]}
        return out


def test_the_feed_is_asked_for_changes_since_a_time_with_the_key_and_read_in_pages(monkeypatch):
    feed = FakeFeed([([{"appid": 1, "last_modified": 100, "price_change_number": 7}], True), ([{"appid": 2, "last_modified": 200}], False)])
    monkeypatch.setattr("shelflife.steam._get", feed)
    changes = fetch_changed_apps("KEY", 12345)
    assert changes == [AppChange(1, 100, 7), AppChange(2, 200, None)]
    first, second = feed.asked
    assert first["key"] == "KEY" and first["if_modified_since"] == 12345 and first["last_appid"] == 0
    assert second["last_appid"] == 1  # the next page starts after the last game of the first


def test_an_empty_answer_is_no_changes(monkeypatch):
    monkeypatch.setattr("shelflife.steam._get", lambda url, params, timeout: {})
    assert fetch_changed_apps("KEY", 1) == []


# ---- noting what changed ----


def test_only_the_games_we_have_are_noted(session):
    add_game(session, 1, "Mine")
    changed = unix(NOW - timedelta(hours=5))
    marked = record_store_changes(session, [AppChange(1, changed, 41), AppChange(999, changed, 5)])
    game = session.get(Game, 1)
    assert marked == 1 and game.price_change_seen == 41
    assert abs((game.store_changed_at - (NOW - timedelta(hours=5))).total_seconds()) < 2
    assert session.get(Game, 999) is None  # nothing is created for a game we do not have


def test_an_older_or_equal_change_is_not_counted_again(session):
    add_game(session, 1)
    when = unix(NOW - timedelta(hours=5))
    assert record_store_changes(session, [AppChange(1, when, 1)]) == 1
    assert record_store_changes(session, [AppChange(1, when, 1)]) == 0
    assert record_store_changes(session, [AppChange(1, when + 60, 2)]) == 1


def test_the_first_check_looks_back_a_month_and_later_ones_since_the_last(session, monkeypatch):
    asked = []
    monkeypatch.setattr(sync, "fetch_changed_apps", lambda key, since, timeout=60: asked.append(since) or [])
    sync_store_changes(session, "KEY")
    first = get_changes_checked(session)
    assert abs(asked[0] - (first - CHANGES_FIRST_DAYS * 86400)) < 5
    sync_store_changes(session, "KEY")
    assert asked[1] == first - CHANGES_OVERLAP_HOURS * HOUR  # with an overlap, so nothing falls between two checks


def test_a_failed_ask_is_not_remembered_so_the_next_one_starts_from_the_same_point(session, monkeypatch):
    set_changes_checked(session, 1000)

    def down(key, since, timeout=60):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(sync, "fetch_changed_apps", down)
    with pytest.raises(requests.ConnectionError):
        sync_store_changes(session, "KEY")
    assert get_changes_checked(session) == 1000


# ---- what is due ----


def test_a_game_that_changed_after_it_was_read_is_due_for_its_details_and_tags(session):
    add_game(session, 1, "Changed", minutes=60)
    add_game(session, 2, "Same", minutes=60)
    for i in (1, 2):
        read(session, i, hours_ago=100)
    record_store_changes(session, [AppChange(1, unix(NOW - timedelta(hours=10)), None)])
    assert [g.steam_app_id for g in details_due(session)] == [1]
    assert 1 in {g.steam_app_id for g in tags_due(session)} and 2 not in {g.steam_app_id for g in tags_due(session)}


def test_a_game_read_after_it_changed_is_not_due(session):
    add_game(session, 1, minutes=60)
    record_store_changes(session, [AppChange(1, unix(NOW - timedelta(hours=10)), None)])
    read(session, 1, hours_ago=1)  # read an hour ago, after the change
    assert details_due(session) == [] and tags_due(session) == []


def test_changed_games_come_most_played_first_and_a_few_at_a_time(session):
    total = CHANGED_BATCH + 5
    for i in range(1, total + 1):
        add_game(session, i, f"G{i}", minutes=i)
        read(session, i, hours_ago=100)
    record_store_changes(session, [AppChange(i, unix(NOW - timedelta(hours=10)), None) for i in range(1, total + 1)])
    due = [g.steam_app_id for g in details_due(session)]
    assert len(due) == CHANGED_BATCH and due[0] == total  # the most played first; the rest wait for the next sync


def test_a_price_number_we_have_not_read_makes_a_price_due_and_reading_it_settles_it(session, monkeypatch):
    add_game(session, 1, "Pool", owned=False)
    game = session.get(Game, 1)
    game.price, game.price_fetched_at = {"final": 999}, NOW  # read just now: not due by age
    session.commit()
    assert prices_due(session) == []
    record_store_changes(session, [AppChange(1, unix(NOW), 55)])
    assert [g.steam_app_id for g in prices_due(session)] == [1]
    monkeypatch.setattr(sync.store, "fetch_prices", lambda ids: {i: {"final": 499} for i in ids})
    assert sync.sync_prices(session) == 1
    game = session.get(Game, 1)
    assert game.price == {"final": 499} and game.price_change_read == 55 and prices_due(session) == []
    record_store_changes(session, [AppChange(1, unix(NOW) + 5, 56)])  # a sale starts: a new number
    assert [g.steam_app_id for g in prices_due(session)] == [1]


# ---- in a sync ----


def test_the_sync_has_a_step_for_it_after_the_wishlist(session, steam):
    assert [key for key, _ in STEPS][:4] == ["library", "wishlist", "changes", "achievements"]


def test_a_sync_asks_the_list_and_says_how_many_of_your_games_changed(session, steam, monkeypatch):
    steam.library = [owned(1, minutes=600)]
    add_game(session, 1, minutes=600)
    read(session, 1, hours_ago=100)
    monkeypatch.setattr(sync, "fetch_changed_apps", lambda key, since, timeout=60: [AppChange(1, unix(NOW - timedelta(hours=3)), 9)])
    result = run_sync(session, "key", "id")
    assert result.changed == 1 and result.changes_note is None
    assert "1 of your games changed on Steam since the last sync" in summarize_sync(result).message
    assert get_changes_checked(session) is not None


@pytest.mark.parametrize("failure", [RateLimited("Steam is rate limiting us."), SteamError("Steam rejected the API key."), requests.ConnectionError("down")])
def test_a_list_that_cannot_be_read_does_not_stop_the_sync(session, steam, monkeypatch, failure):
    steam.library = [owned(1, minutes=600)]

    def fail(key, since, timeout=60):
        raise failure

    monkeypatch.setattr(sync, "fetch_changed_apps", fail)
    result = run_sync(session, "key", "id")
    assert result.aborted is None and result.changed == 0 and result.changes_note is not None
    assert result.details == 1  # the rest of the sync still ran
    assert "Steam's list of changes could not be read" in summarize_sync(result).message
    assert get_changes_checked(session) is None


def test_nothing_about_changes_is_said_when_nothing_changed():
    assert "changed on Steam" not in summarize_sync(SyncResult(games=3)).message
