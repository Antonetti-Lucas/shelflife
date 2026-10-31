"""Problems found reviewing the wishlist, sync and search work together."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from shelflife import sync_job
from shelflife.discovery import pending_pool
from shelflife.models import OFF_TOPIC, Game
from shelflife.steam import OwnedGame
from shelflife.sync import details_due, upsert_library
from shelflife.sync_job import what_is_due
from shelflife.web.app import app, parse_app_id
from shelflife.wishlist import WishlistEntry, import_wishlist
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def due_ids(session):
    return [g.steam_app_id for g in details_due(session)]


# ---- a game search leaves the wishlist alone ----


def test_a_game_search_does_not_fetch_details_for_wishlist_games(session):
    import_wishlist(session, [WishlistEntry(7, 1, None), WishlistEntry(8, 0, None)])
    add_game(session, 9, "From a search", owned=False)
    session.get(Game, 9).details_fetched_at = None
    session.commit()
    assert [g.steam_app_id for g in pending_pool(session)] == [9]


# ---- what a sync retries ----


def test_games_with_no_tags_are_retried_but_dlc_and_software_are_not(session):
    for app_id, kind in ((1, "game"), (2, "dlc"), (3, "software"), (4, "unavailable")):
        add_game(session, app_id, f"G{app_id}", type=kind, tags={})
        session.get(Game, app_id).details_fetched_at = datetime.now() - timedelta(days=10)  # past the retry age, not yet stale
    session.commit()
    assert sorted(due_ids(session)) == [1, 4]


# ---- a game a search set aside, then wanted ----


def set_aside(session, app_id):
    add_game(session, app_id, f"Aside {app_id}", type=OFF_TOPIC, tags={"Stealth": 5}, owned=False)
    session.get(Game, app_id).details_fetched_at = datetime.now()
    session.commit()


def test_a_game_you_wishlist_after_a_search_set_it_aside_is_looked_up_again(session):
    set_aside(session, 50)
    import_wishlist(session, [WishlistEntry(50, 3, None)])
    game = session.get(Game, 50)
    assert game.type is None and game.details_fetched_at is None
    assert 50 in due_ids(session)


def test_a_game_you_buy_after_a_search_set_it_aside_is_looked_up_again(session):
    set_aside(session, 60)
    upsert_library(session, [OwnedGame(app_id=60, name="Aside 60", playtime_total_min=10, playtime_recent_min=0, last_played_ts=0)])
    game = session.get(Game, 60)
    assert game.type is None and game.details_fetched_at is None
    assert 60 in due_ids(session)


def test_a_game_that_was_set_aside_and_stays_unwanted_is_left_alone(session):
    set_aside(session, 70)
    import_wishlist(session, [WishlistEntry(7, 1, None)])
    assert session.get(Game, 70).type == OFF_TOPIC


# ---- a wishlist that lists a game twice ----


def test_a_game_listed_twice_on_the_wishlist_is_saved_once(session):
    result = import_wishlist(session, [WishlistEntry(7, 2, None), WishlistEntry(7, 5, None), WishlistEntry(8, 0, None)])
    assert (result.total, result.added) == (2, 2)


# ---- a game id typed into a form ----


@pytest.mark.parametrize("raw, expected", [("12", 12), (" 12 ", 12), ("", None), ("abc", None), ("²", None), ("9" * 30, None), ("-4", None), ("1.5", None)])
def test_only_a_plain_number_is_a_game_id(raw, expected):
    assert parse_app_id(raw) == expected


@pytest.mark.parametrize("raw", ["²", "9" * 30])
def test_a_game_id_that_is_not_one_gets_a_message_not_a_server_error(client, raw):
    response = client.post("/explore/find", data={"like": raw}, headers=SAME_SITE, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/explore", params={"like": raw}).status_code == 200


# ---- the Sync page while a sync runs ----


def test_the_due_numbers_can_be_left_out(session, monkeypatch):
    add_game(session, 1, "Played", minutes=60)

    def fail(*args, **kwargs):
        raise AssertionError("worked out what is due")

    monkeypatch.setattr(sync_job, "achievements_due", fail)
    monkeypatch.setattr(sync_job, "details_due", fail)
    monkeypatch.setattr(sync_job, "reviews_due", fail)
    quick = what_is_due(session, with_due=False)
    assert quick["library"] == 1 and quick["achievements"] is None and quick["minutes"] is None


def test_the_sync_page_does_not_work_out_what_is_due_while_a_sync_runs(client, monkeypatch):
    monkeypatch.setattr(sync_job.job, "snapshot", lambda: sync_job.JobSnapshot(state="running", step=1, steps=5, text="x"))
    seen = []
    real = what_is_due
    monkeypatch.setattr("shelflife.web.app.what_is_due", lambda session, with_due=True: seen.append(with_due) or real(session, with_due))
    html = client.get("/sync").text
    assert seen == [False] and "Due now" not in html and "Nothing is due" not in html


def test_the_sync_page_still_says_what_is_due_when_idle(client):
    html = client.get("/sync").text
    assert "Nothing is due" in html
