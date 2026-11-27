"""A game's lowest price ever on Steam, from IsThereAnyDeal: asked for, kept, refreshed and shown."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from shelflife import itad, prices, sync
from shelflife.models import Game, WishlistItem
from shelflife.sync import lows_due, sync_lows
from shelflife.sync_job import STEPS, SyncResult, summarize_sync
from shelflife.web import app as web_app
from tests.conftest import FakeResponse
from tests.factories import add_game

BRL = {"currency": "BRL", "initial": 5999, "final": 5999, "discount": 0}


def steam_low(cents, cut, when="2025-03-14T02:00:00+01:00", currency="BRL"):
    return {"shop": {"id": 61, "name": "Steam"}, "price": {"amount": cents / 100, "amountInt": cents, "currency": currency},
            "regular": {"amount": 59.99, "amountInt": 5999, "currency": currency}, "cut": cut, "timestamp": when}


@pytest.fixture
def api(monkeypatch):
    """A fake IsThereAnyDeal: app 1 and 2 are known (2 never on sale), app 3 isn't."""
    monkeypatch.setenv("ITAD_API_KEY", "k")
    monkeypatch.setenv("STEAM_COUNTRY", "br")
    calls = []

    def post(url, json=None, params=None, headers=None, timeout=None):
        calls.append((url, json, params))
        assert headers["ITAD-API-Key"] == "k"
        if "/lookup/id/shop/61/v1" in url:
            return FakeResponse(200, {key: {"app/1": "g1", "app/2": "g2"}.get(key) for key in json})
        assert url.endswith("/games/storelow/v2") and params == {"country": "BR", "shops": 61}
        lows = {"g1": [{"shop": {"id": 35, "name": "GOG"}, "price": {"amountInt": 100, "currency": "BRL"}, "cut": 98, "timestamp": "2024-01-01"},
                       steam_low(1499, 75)],
                "g2": [steam_low(5999, 0)]}
        return FakeResponse(200, [{"id": g, "lows": lows[g]} for g in json if g in lows])

    monkeypatch.setattr(itad.requests, "post", post)
    return calls


def pool_game(session, app_id, price=BRL, **kw):
    game = add_game(session, app_id, owned=False, tags={"RPG": 10}, **kw)
    game.price = price
    session.commit()
    return game


# ---- reading IsThereAnyDeal ----


def test_steam_ids_are_turned_into_its_ids(api):
    assert itad.lookup([1, 3]) == {1: "g1", 3: None}
    assert api[0][1] == ["app/1", "app/3"]


def test_only_steams_low_is_kept(api):
    assert itad.steam_lows(["g1"]) == {"g1": {"cents": 1499, "regular": 5999, "currency": "BRL", "cut": 75, "date": "2025-03-14"}}


def test_without_a_key_nothing_is_asked(session):
    pool_game(session, 1)
    assert not itad.configured() and sync_lows(session) == 0


def test_a_refused_key_says_so(monkeypatch):
    monkeypatch.setenv("ITAD_API_KEY", "bad")
    monkeypatch.setattr(itad.requests, "post", lambda *a, **k: FakeResponse(403, {}))
    with pytest.raises(itad.ItadError, match="ITAD_API_KEY"):
        itad.lookup([1])


# ---- Sync ----


def test_sync_keeps_the_low_and_asks_for_ids_once(session, api):
    pool_game(session, 1)
    pool_game(session, 2)
    pool_game(session, 3)
    assert sync_lows(session) == 3
    one, two, three = (session.get(Game, i) for i in (1, 2, 3))
    assert one.itad_id == "g1" and one.price_low["cents"] == 1499
    assert two.price_low["cut"] == 0
    assert three.itad_id == "" and three.price_low == {}  # IsThereAnyDeal has no such game: not asked again
    assert sync_lows(session) == 0  # nothing due a moment later
    assert sync_lows(session, everything=True) == 2  # the one it has no id for stays out
    assert sum("lookup" in c[0] for c in api) == 1


def test_your_games_free_games_and_games_without_a_price_are_not_asked_about(session, api):
    add_game(session, 1, tags={"RPG": 10})  # yours
    pool_game(session, 2, price={"free": True})
    pool_game(session, 3, price=None)
    assert lows_due(session) == []


def test_the_wishlist_is_read_every_day_the_rest_every_week(session, api):
    pool_game(session, 1)
    pool_game(session, 2)
    session.add(WishlistItem(steam_app_id=1, priority=0, added_at=datetime(2026, 1, 1), seen_at=datetime(2026, 1, 1)))
    session.commit()
    sync_lows(session)
    for g in (session.get(Game, 1), session.get(Game, 2)):
        g.price_low_fetched_at -= timedelta(days=2)
    session.commit()
    assert [g.steam_app_id for g in lows_due(session)] == [1]


def test_it_is_a_sync_step_and_its_failure_is_only_noted():
    assert STEPS[-1][0] == "lows"
    message = summarize_sync(SyncResult(games=3, lows=40, lows_note="could not be asked (down)")).message
    assert "lowest prices for 40 games" in message and "IsThereAnyDeal could not be asked" in message


# ---- showing it ----


def low_game(cents=1499, cut=75, final=5999, discount=0, currency="BRL"):
    game = Game(steam_app_id=9, name="x")
    game.price = {"currency": "BRL", "initial": 5999, "final": final, "discount": discount}
    game.price_low = {"cents": cents, "regular": 5999, "currency": currency, "cut": cut, "date": "2025-03-14"}
    return game


def test_the_low_reads_with_its_month():
    assert prices.low_of(low_game()) == prices.Low("R$ 14,99", 75, "Mar 2025", False)


def test_a_sale_at_the_low_is_flagged():
    assert prices.low_of(low_game(final=1499, discount=75)).now
    assert not prices.low_of(low_game(final=1999, discount=67)).now


def test_a_low_in_another_currency_is_not_shown():
    assert prices.low_of(low_game(currency="USD")) is None


@pytest.fixture
def client():
    with TestClient(web_app.app) as c:
        yield c


def test_the_game_page_shows_the_low_and_credits_isthereanydeal(client, session):
    game = pool_game(session, 1)
    game.price_low = {"cents": 1499, "regular": 5999, "currency": "BRL", "cut": 75, "date": "2025-03-14"}
    session.commit()
    page = client.get("/game/1").text
    assert "Lowest on Steam: <strong>R$ 14,99</strong> (-75%), Mar 2025." in page and "isthereanydeal.com" in page
    game.price = {"currency": "BRL", "initial": 5999, "final": 1499, "discount": 75}
    session.commit()
    page = client.get("/game/1").text
    assert "Its lowest price ever on Steam." in page and "Lowest ever</span>" in page


def test_a_game_you_own_shows_no_low(client, session):
    game = add_game(session, 1, tags={"RPG": 10})
    game.price, game.price_low = BRL, {"cents": 1499, "regular": 5999, "currency": "BRL", "cut": 75, "date": "2025-03-14"}
    session.commit()
    assert "Lowest on Steam" not in client.get("/game/1").text
