"""Prices on screen, and the store details on game pages: who made it, released or not,
screenshots, the store background."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from shelflife import store
from shelflife.models import Game
from shelflife.prices import money, shown
from shelflife.web.app import app
from tests.factories import add_game, build_scenario

SALE = {"currency": "BRL", "initial": 7499, "final": 1874, "discount": 75}


@pytest.mark.parametrize("cents, currency, text", [
    (7399, "BRL", "R$ 73,99"), (123456, "BRL", "R$ 1.234,56"), (1999, "USD", "$19.99"),
    (1999, "EUR", "19,99 €"), (500, "XYZ", "5.00 XYZ"), (1200, "JPY", "¥12"),
])
def test_money_is_written_the_local_way(cents, currency, text):
    assert money(cents, currency) == text


def test_a_sale_shows_the_discount_and_the_full_price():
    view = shown(SALE)
    assert (view.text, view.was, view.discount) == ("R$ 18,74", "R$ 74,99", 75)
    assert shown({"free": True}).text == "Free"
    assert shown({}) is None and shown(None) is None


@pytest.fixture
def client(session):
    build_scenario(session)
    now = datetime.now()
    pool = session.get(Game, 40)  # Pool Stealth Good: not owned
    pool.price, pool.price_fetched_at = SALE, now
    pool.store_info = {
        "developers": ["Shadow Studio"], "publishers": ["Big Publisher"], "coming_soon": True,
        "screenshots": ["https://example.com/ss_1.1920x1080.jpg", "https://example.com/ss_2.1920x1080.jpg"],
        "background": "https://example.com/page_bg_raw.jpg",
    }
    owned = session.get(Game, 20)  # Backlog Stealth: owned
    owned.price, owned.price_fetched_at = {"currency": "BRL", "initial": 100, "final": 100, "discount": 0}, now
    session.commit()
    with TestClient(app) as c:
        yield c


def test_cards_show_prices_of_games_you_dont_own_only(client):
    home = client.get("/").text
    assert 'data-price-id="40"' in home and "R$ 18,74" in home and "-75%" in home and "R$ 74,99" in home
    assert 'data-price-id="20"' not in home  # you own it: no price


def test_the_game_page_shows_who_made_it_release_screenshots_and_background(client):
    html = client.get("/game/40").text
    assert "By <strong>Shadow Studio</strong>" in html and "Published by <strong>Big Publisher</strong>" in html
    assert "Coming soon" in html and "store-bg" in html and "page_bg_raw.jpg" in html
    assert html.count('class="shot"') == 2 and "ss_1.600x338.jpg" in html  # small pictures, the full one on click
    assert "R$ 18,74" in html


def test_an_unreleased_game_is_flagged_on_its_card(client):
    assert 'class="flag soon">Coming soon' in client.get("/explore").text


def test_prices_on_screen_are_read_again_when_old_and_only_then(client, session, monkeypatch):
    asked = []
    monkeypatch.setattr(store, "fetch_prices", lambda ids: asked.append(sorted(ids)) or {i: {"free": True} for i in ids})
    old = session.get(Game, 41)  # Pool Stealth Bad: never priced
    assert client.get("/prices?ids=40,41,20,abc").json() == {"40": shown(SALE).as_dict(), "41": {"text": "Free", "was": None, "discount": 0}}
    assert asked == [[41]]  # 40 is fresh, 20 is yours, abc is not an id
    session.expire_all()
    pool = session.get(Game, 40)
    pool.price_fetched_at = datetime.now() - timedelta(days=2)
    session.commit()
    client.get("/prices?ids=40")
    assert asked[-1] == [40]


def test_when_the_store_cant_be_reached_the_stored_prices_are_shown(client, monkeypatch):
    def down(ids):
        raise store.requests.ConnectionError("offline")

    monkeypatch.setattr(store, "fetch_prices", down)
    assert client.get("/prices?ids=40,41").json() == {"40": shown(SALE).as_dict(), "41": None}
