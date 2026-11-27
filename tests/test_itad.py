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
        if url.endswith("/games/prices/v3"):  # when each sale began: none known here
            return FakeResponse(200, [])
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


# ---- a new record, and the low it beat ----


def test_a_lower_low_remembers_the_one_it_replaced():
    old = {"cents": 1999, "currency": "BRL", "cut": 67, "date": "2024-11-20"}
    new = {"cents": 1499, "currency": "BRL", "cut": 75, "date": "2026-11-25"}
    kept = itad.keep_before(old, new, datetime(2026, 11, 26))
    assert kept["before"] == {"cents": 1999, "date": "2024-11-20"} and kept["seen"] == "2026-11-26"
    assert itad.keep_before(kept, dict(new), datetime(2026, 12, 3))["seen"] == "2026-11-26"  # same low: kept as it was
    assert "before" not in itad.keep_before(None, new, datetime(2026, 11, 26))  # the first read: nothing to compare


def test_a_sale_under_the_low_is_a_record_until_sync_catches_up():
    low = prices.low_of(low_game(final=999, discount=83))
    assert low.record and low.before == "R$ 14,99, Mar 2025"


def test_a_record_names_the_low_it_beat_when_a_sync_saw_it():
    game = low_game(final=1499, discount=75)
    game.price_low |= {"date": "2026-11-20", "before": {"cents": 1999, "date": "2024-11-20"},
                       "sale_since": "2026-11-20T19:00:00+01:00", "sale_until": "2026-12-04T19:00:00+01:00", "sale_for": 1499}
    fresh = prices.low_of(game, today=datetime(2026, 11, 26).date())
    assert fresh.record and fresh.before == "R$ 19,99, Nov 2024"
    game.price_low |= {"sale_since": "2027-02-25T19:00:00+01:00", "sale_until": "2027-03-04T19:00:00+01:00"}
    later = prices.low_of(game, today=datetime(2027, 3, 1).date())  # a later sale only matches it
    assert later.now and not later.record


def test_flags_and_credit_on_the_pages(client, session, monkeypatch):
    game = pool_game(session, 1)
    game.price = {"currency": "BRL", "initial": 5999, "final": 999, "discount": 83}
    game.price_low = {"cents": 1499, "regular": 5999, "currency": "BRL", "cut": 75, "date": "2025-03-14"}
    session.commit()
    page = client.get("/game/1").text
    assert "Never this cheap on Steam before" in page and "New lowest</span>" in page
    assert "IsThereAnyDeal API" in page and "Not affiliated" not in page  # the footer needs a key
    monkeypatch.setenv("ITAD_API_KEY", "k")
    assert "Lowest prices from the" in client.get("/game/1").text


# ---- the lowest in two years ----


def test_a_sale_at_its_two_year_low_is_amber():
    game = low_game(final=2349, discount=50)
    game.price_low |= {"y2": 2349}
    low = prices.low_of(game)
    assert low.two_years and not low.now
    game.price_low["y2"] = 1999
    assert not prices.low_of(game).two_years


def test_sync_reads_the_two_year_low_only_for_sales_above_the_low(session, api, monkeypatch):
    asked = []
    monkeypatch.setattr(itad, "steam_low_since", lambda gid, since: asked.append(gid) or (2349, "BRL"))
    pool_game(session, 1, price={"currency": "BRL", "initial": 5999, "final": 2349, "discount": 61})  # on sale above its low
    pool_game(session, 2, price={"currency": "BRL", "initial": 5999, "final": 2999, "discount": 50})  # low is 5999: at/below it
    pool_game(session, 4)  # not on sale
    sync_lows(session)
    assert asked == ["g1"] and session.get(Game, 1).price_low["y2"] == 2349
    sync_lows(session, everything=True)
    assert asked == ["g1"]  # once per sale, not every sync


def test_the_two_year_low_survives_the_next_read_of_the_same_low():
    old = {"cents": 1499, "currency": "BRL", "cut": 75, "date": "2025-03-14", "y2": 2349, "y2_read": "2026-11-26"}
    assert itad.keep_before(old, {"cents": 1499, "currency": "BRL", "cut": 75, "date": "2025-03-14"}, datetime(2026, 11, 27))["y2"] == 2349


def test_the_history_is_steams_only(monkeypatch):
    monkeypatch.setenv("ITAD_API_KEY", "k")
    seen = {}

    def get(url, params=None, headers=None, timeout=None):
        seen.update(params)
        return FakeResponse(200, [
            {"timestamp": "2026-06-25", "shop": {"id": 61}, "deal": {"price": {"amountInt": 2349, "currency": "BRL"}}},
            {"timestamp": "2026-03-26", "shop": {"id": 61}, "deal": {"price": {"amountInt": 4699, "currency": "BRL"}}},
        ])

    monkeypatch.setattr(itad.requests, "get", get)
    assert itad.steam_low_since("g1", datetime(2024, 11, 26)) == (2349, "BRL")
    assert seen["shops"] == 61 and seen["since"] == "2024-11-26T00:00:00Z"


def test_a_new_sale_price_asks_for_the_two_year_low_again(session, api, monkeypatch):
    asked = []
    monkeypatch.setattr(itad, "steam_low_since", lambda gid, since: asked.append(gid) or (2349, "BRL"))
    game = pool_game(session, 1, price={"currency": "BRL", "initial": 5999, "final": 2349, "discount": 61})
    sync_lows(session)
    game.price = {"currency": "BRL", "initial": 5999, "final": 1999, "discount": 67}
    session.commit()
    sync_lows(session, everything=True)
    assert asked == ["g1", "g1"]


# ---- sorting and filtering by the low ----


def priced(session, app_id, final, discount, low_cents, wish=False, **low):
    game = pool_game(session, app_id, price={"currency": "BRL", "initial": 5999, "final": final, "discount": discount})
    game.name = f"Game {app_id}"
    game.price_low = {"cents": low_cents, "regular": 5999, "currency": "BRL", "cut": 50, "date": "2025-03-14", **low} if low_cents else None
    if wish:
        session.add(WishlistItem(steam_app_id=app_id, priority=0, added_at=datetime(2026, 1, 1), seen_at=datetime(2026, 1, 1)))
    session.commit()
    return game


def test_the_price_filter_keeps_games_at_a_low(session):
    at_ever = priced(session, 1, 1499, 75, 1499)
    at_two_years = priced(session, 2, 2349, 61, 1499, y2=2349)
    on_sale = priced(session, 3, 2999, 50, 1499, y2=1999)
    full = priced(session, 4, 5999, 0, 1499)
    kept = lambda sale: [g.steam_app_id for g in (at_ever, at_two_years, on_sale, full) if prices.keeps_sale(g, sale)]  # noqa: E731
    assert kept("") == [1, 2, 3, 4] and kept("1") == [1, 2, 3] and kept("low") == [1, 2] and kept("ever") == [1]
    record = priced(session, 5, 999, 83, 1499)
    assert prices.keeps_sale(record, "new") and not prices.keeps_sale(at_ever, "new")


def test_closest_to_its_lowest_ever_comes_first(session):
    games = [priced(session, 1, 2999, 50, 1499), priced(session, 2, 1499, 75, 1499), priced(session, 3, 5999, 0, None), priced(session, 4, 1999, 67, 1499)]
    assert [g.steam_app_id for g in sorted(games, key=prices.low_key)] == [2, 4, 1, 3]


def test_wishlist_and_explore_sort_and_filter_by_the_low(client, session):
    priced(session, 1, 2999, 50, 1499, wish=True)
    priced(session, 2, 1499, 75, 1499, wish=True)
    page = client.get("/wishlist?sort=low&sale=ever").text
    assert "Game 2" in page and "Game 1" not in page and "Closest to its lowest ever" in page
    page = client.get("/explore?sort=low").text
    assert page.index("Game 2") < page.index("Game 1")


def test_on_a_card_the_low_flag_sits_on_the_cover(client, session):
    priced(session, 2, 1499, 75, 1499, wish=True)
    page = client.get("/wishlist").text
    assert 'class="low-tag  flag bottom"' in page or 'low-tag flag bottom' in " ".join(page.split())


def test_a_sale_at_a_low_first_reached_this_sale_is_a_new_lowest():
    # Live, 2026-11-26: FF7 Remake Intergrade's -82% low was first reached 2026-11-18, the day its sale began
    game = low_game(cents=3148, final=3148, discount=82)
    game.price_low |= {"date": "2026-11-18", "sale_since": "2026-11-18T19:17:43+01:00", "sale_until": "2026-12-02T19:00:00+01:00", "sale_for": 3148}
    today = datetime(2026, 11, 26).date()
    low = prices.low_of(game, today=today)
    assert low.record and low.before is None
    game.price_low["date"] = "2024-03-14"  # Zuma Deluxe: a low from an older sale, only matched
    assert not prices.low_of(game, today=today).record
    game.price_low["date"] = "2026-11-18"
    assert not prices.low_of(game, today=datetime(2026, 12, 5).date()).record  # that sale is over: can't tell
    game.price_low["sale_for"] = 2999
    assert not prices.low_of(game, today=today).record  # the sale read was at another price


def test_a_sale_longer_than_two_weeks_still_counts():
    game = low_game(cents=3148, final=3148, discount=82)
    game.price_low |= {"date": "2026-10-01", "sale_since": "2026-10-01T19:00:00+01:00", "sale_until": None, "sale_for": 3148}
    assert prices.low_of(game, today=datetime(2026, 11, 26).date()).record


def test_sync_reads_when_each_sale_began(session, api, monkeypatch):
    monkeypatch.setattr(itad, "steam_sales", lambda ids: {"g1": {"since": "2026-11-18T19:17:43+01:00", "until": "2026-12-02T19:00:00+01:00", "cents": 1499}})
    pool_game(session, 1, price={"currency": "BRL", "initial": 5999, "final": 1499, "discount": 75})
    sync_lows(session)
    assert session.get(Game, 1).price_low["sale_since"].startswith("2026-11-18")


def test_sale_starts_are_steams_only(monkeypatch):
    monkeypatch.setenv("ITAD_API_KEY", "k")
    answer = [
        {"id": "g1", "deals": [{"shop": {"id": 35}, "cut": 90, "timestamp": "2026-01-01", "price": {"amountInt": 1}},
                               {"shop": {"id": 61}, "cut": 82, "timestamp": "2026-11-18T19:17:43+01:00", "expiry": "2026-12-02T19:00:00+01:00", "price": {"amountInt": 3148}}]},
        {"id": "g2", "deals": [{"shop": {"id": 61}, "cut": 0, "timestamp": "2026-07-09", "price": {"amountInt": 4699}}]},
    ]
    monkeypatch.setattr(itad.requests, "post", lambda url, json=None, params=None, headers=None, timeout=None: FakeResponse(200, answer))
    assert itad.steam_sales(["g1", "g2"]) == {"g1": {"since": "2026-11-18T19:17:43+01:00", "until": "2026-12-02T19:00:00+01:00", "cents": 3148}}



def test_a_game_at_its_low_with_no_sale_start_is_due_at_once(session, api):
    pool_game(session, 1, price={"currency": "BRL", "initial": 5999, "final": 1499, "discount": 75})
    pool_game(session, 2, price={"currency": "BRL", "initial": 5999, "final": 2999, "discount": 50})
    sync_lows(session)
    assert lows_due(session) == []  # each sale asked about once, even when no sale start was found
    for game in (session.get(Game, 1), session.get(Game, 2)):  # lows read before sale starts were kept
        game.price_low = {k: v for k, v in game.price_low.items() if not k.startswith("sale_")}
    session.commit()
    assert [g.steam_app_id for g in lows_due(session)] == [1]  # at its low: due again; 2 is under it, new anyway
