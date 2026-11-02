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
