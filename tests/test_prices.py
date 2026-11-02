"""Store prices: kept from the same store call as the other details, for every game."""

from datetime import datetime

import pytest

from shelflife import store, sync
from shelflife.models import Game
from tests.factories import add_game


@pytest.mark.parametrize("data, price", [
    ({"is_free": True}, {"free": True}),
    ({"is_free": False, "price_overview": {"currency": "BRL", "initial": 5999, "final": 2999, "discount_percent": 50}},
     {"currency": "BRL", "initial": 5999, "final": 2999, "discount": 50}),
    ({"price_overview": {"currency": "USD", "final": 1999}}, {"currency": "USD", "initial": 1999, "final": 1999, "discount": 0}),
    ({}, {}),  # not released yet, or not sold any more
    ({"price_overview": {"final": 100}}, {}),  # no currency: not a usable price
])
def test_the_store_price_is_kept_as_shown(data, price):
    assert store.parse_price(data) == price


def read_before_prices(session, app_id):
    """A game whose details, text and languages were read before prices were kept."""
    game = session.get(Game, app_id)
    game.details_fetched_at, game.about_text, game.languages = datetime(2026, 1, 1), "words", {"English": "text"}
    game.store_info = {}
    session.commit()


def test_every_game_is_due_once_for_its_price_played_or_not(session):
    add_game(session, 1, "Played", minutes=3000)
    add_game(session, 2, "Pool", owned=False)
    add_game(session, 3, "Priced", owned=False)
    for app_id in (1, 2, 3):
        read_before_prices(session, app_id)
    session.get(Game, 3).price_fetched_at = datetime(2026, 1, 1)
    session.commit()
    assert [g.steam_app_id for g in sync.about_due(session)] == [1, 2]


def test_the_catch_up_stores_the_price(session, monkeypatch):
    add_game(session, 1, "Played", minutes=3000)
    read_before_prices(session, 1)
    monkeypatch.setattr(store, "fetch_store_details", lambda app_id, timeout=15: store.StoreDetails(
        type="game", short_description="d", about_text="words", price={"free": True}))
    assert sync.sync_about(session) == 1
    game = session.get(Game, 1)
    assert game.price == {"free": True} and game.price_fetched_at is not None and sync.about_due(session) == []


# ---- the other store details, kept for later ----


def test_the_other_store_details_are_kept_in_short_form():
    data = {
        "developers": ["Supergiant Games"], "publishers": ["Supergiant Games"],
        "categories": [{"id": 2, "description": "Single-player"}, {"id": 22, "description": "Steam Achievements"}],
        "platforms": {"windows": True, "mac": True, "linux": False}, "controller_support": "full",
        "metacritic": {"score": 93, "url": "https://example.com"}, "required_age": "17",
        "content_descriptors": {"ids": [2, 5], "notes": "Violence"}, "release_date": {"coming_soon": False, "date": "17 Sep, 2020"},
        "dlc": [1, 2], "achievements": {"total": 49, "highlighted": []}, "recommendations": {"total": 287696},
        "screenshots": [{"path_full": f"https://example.com/{i}.jpg"} for i in range(10)],
        "website": "http://www.supergiantgames.com", "background_raw": "https://example.com/bg.jpg",
    }
    info = store.parse_info(data)
    assert info["developers"] == ["Supergiant Games"] and info["categories"] == ["Single-player", "Steam Achievements"]
    assert info["platforms"] == ["windows", "mac"] and info["metacritic"] == 93 and info["required_age"] == 17
    assert info["content_descriptors"] == [2, 5] and info["coming_soon"] is False and info["dlc_count"] == 2
    assert info["achievements"] == 49 and info["recommendations"] == 287696
    assert len(info["screenshots"]) == store.SCREENSHOTS_KEPT and info["background"] == "https://example.com/bg.jpg"


def test_a_sparse_store_answer_keeps_only_what_is_there():
    info = store.parse_info({"required_age": "not a number"})
    assert info == {"developers": [], "publishers": [], "categories": [], "platforms": [],
                    "coming_soon": False, "dlc_count": 0, "screenshots": []}


def test_a_game_read_before_the_other_details_were_kept_is_due_once(session):
    add_game(session, 1, "Played", minutes=3000)
    read_before_prices(session, 1)
    game = session.get(Game, 1)
    game.price_fetched_at, game.store_info = datetime(2026, 1, 1), None
    session.commit()
    assert [g.steam_app_id for g in sync.about_due(session)] == [1]


@pytest.mark.parametrize("country, cc", [("BR", "br"), ("", None)])
def test_your_country_sets_the_currency(monkeypatch, country, cc):
    asked = {}

    class Answer:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"1": {"success": False}}

    def fake_get(url, params=None, **kwargs):
        asked.update(params)
        return Answer()

    monkeypatch.setenv("STEAM_COUNTRY", country)
    monkeypatch.setattr(store.requests, "get", fake_get)
    monkeypatch.setattr(store.store_throttle, "wait", lambda: None)
    assert store.fetch_store_details(1) is None
    assert asked.get("cc") == cc
