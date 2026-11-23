"""Explore and the Wishlist can be sorted by lowest price or biggest discount, and narrowed to the games
on sale now. The prices are the ones last read (Game.price)."""

import re

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.models import Game
from shelflife.prices import discount_key, discount_percent, final_cents, price_key
from shelflife.web.app import app
from shelflife.wishlist import WishlistEntry, import_wishlist
from tests.factories import add_game

PRICES = {  # name -> stored price
    "Free One": {"free": True},
    "Cheap": {"currency": "BRL", "initial": 2900, "final": 2900, "discount": 0},
    "Dear": {"currency": "BRL", "initial": 20000, "final": 20000, "discount": 0},
    "Sale Big": {"currency": "BRL", "initial": 10000, "final": 2500, "discount": 75},
    "Sale Small": {"currency": "BRL", "initial": 3000, "final": 2700, "discount": 10},
    "Unknown": None,
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pool(session):
    """A liked game, and games you do not own with these prices, all like it."""
    add_game(session, 1, "Loved", tags={"Stealth": 100, "Indie": 50}, minutes=900)
    diary.save_rating(session, 1, 5.0)
    for i, (name, price) in enumerate(PRICES.items(), start=10):
        add_game(session, i, name, tags={"Stealth": 90, "Indie": 40}, owned=False, reviews=(900, 100))
        session.get(Game, i).price = price
    session.commit()
    import_wishlist(session, [WishlistEntry(i, 0, None) for i in range(10, 10 + len(PRICES))])


def titles(html):
    return re.findall(r'<h3><a href="/game/\d+">([^<]+)</a></h3>', html)


# ---- the ordering rules ----


def test_the_cheapest_comes_first_free_ones_before_all_and_unknown_prices_last():
    names = sorted(PRICES, key=lambda n: price_key(PRICES[n], n))
    assert names == ["Free One", "Sale Big", "Sale Small", "Cheap", "Dear", "Unknown"]


def test_the_biggest_discount_comes_first_and_games_not_on_sale_last_cheapest_first():
    names = sorted(PRICES, key=lambda n: discount_key(PRICES[n], n))
    assert names == ["Sale Big", "Sale Small", "Free One", "Cheap", "Dear", "Unknown"]


@pytest.mark.parametrize(
    "price, cents, percent",
    [
        (None, None, 0),
        ({}, None, 0),
        ({"free": True}, 0, 0),
        ({"currency": "USD", "initial": 1000, "final": 500, "discount": 50}, 500, 50),
        ({"currency": "USD", "final": 500}, 500, 0),
    ],
)
def test_what_a_stored_price_means_for_sorting(price, cents, percent):
    assert (final_cents(price), discount_percent(price)) == (cents, percent)


# ---- Explore ----


def test_explore_sorts_by_lowest_price(client, pool):
    html = client.get("/explore?sort=price").text
    assert titles(html) == ["Free One", "Sale Big", "Sale Small", "Cheap", "Dear", "Unknown"]


def test_explore_sorts_by_biggest_discount(client, pool):
    html = client.get("/explore?sort=discount").text
    assert titles(html)[:3] == ["Sale Big", "Sale Small", "Free One"]


def test_explore_can_show_only_games_on_sale(client, pool):
    html = client.get("/explore?sale=1&sort=discount").text
    assert titles(html) == ["Sale Big", "Sale Small"]
    assert 'name="sale"' in html and 'value="1" selected' in html


def test_the_sort_and_the_filter_are_offered_and_kept_while_paging(client, pool):
    html = client.get("/explore?sale=1&sort=price").text
    assert "Lowest price" in html and "Biggest discount" in html and "On sale now" in html
    assert "sale=1" in html.split('"pager')[-1] or "&sale=1&" in html  # the page links keep the filter


def test_an_unknown_value_for_the_filter_means_any_price(client, pool):
    assert len(titles(client.get("/explore?sale=banana").text)) == len(PRICES)


def test_like_this_search_also_sorts_by_price(client, pool):
    html = client.get("/explore?like=1&sort=price").text
    assert titles(html)[0] == "Free One"


# ---- the Wishlist ----


def test_the_wishlist_sorts_by_lowest_price_and_biggest_discount(client, pool):
    assert titles(client.get("/wishlist?sort=price").text) == ["Free One", "Sale Big", "Sale Small", "Cheap", "Dear", "Unknown"]
    assert titles(client.get("/wishlist?sort=discount").text)[:2] == ["Sale Big", "Sale Small"]


def test_the_wishlist_can_show_only_games_on_sale(client, pool):
    assert titles(client.get("/wishlist?sale=1&sort=discount").text) == ["Sale Big", "Sale Small"]
    assert "On sale now" in client.get("/wishlist").text


def test_nothing_on_sale_says_no_games_match(client, session, pool):
    for g in session.query(Game).all():
        if g.price and g.price.get("discount"):
            g.price = {**g.price, "discount": 0}
    session.commit()
    assert "No games match" in client.get("/wishlist?sale=1").text
