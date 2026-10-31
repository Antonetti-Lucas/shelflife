"""Your wishlist: reading it from Steam, keeping it in step, and putting it in order."""

from datetime import datetime, timezone

import pytest
import requests

from shelflife import wishlist
from shelflife.models import Game, WishlistItem
from shelflife.throttle import RateLimited
from shelflife.wishlist import WishlistEntry, fetch_wishlist, import_wishlist, order_key, rank_label
from tests.conftest import FakeResponse
from tests.factories import add_game

JAN_1_2020 = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def http(monkeypatch):
    calls = []
    answer = {"response": FakeResponse(200, {"response": {"items": []}})}

    def get(url, params=None, **kwargs):
        calls.append((url, params))
        return answer["response"]

    monkeypatch.setattr(wishlist.requests, "get", get)
    get.calls, get.answer = calls, answer
    return get


def entry(app_id, priority=0, added=None):
    return WishlistEntry(app_id, priority, added)


# ---- reading it from Steam ----


def test_the_wishlist_is_read_with_ranks_and_dates(http):
    http.answer["response"] = FakeResponse(
        200, {"response": {"items": [{"appid": 7, "priority": 12, "date_added": JAN_1_2020}, {"appid": 8, "priority": 0, "date_added": JAN_1_2020 + 86400}]}}
    )
    entries = fetch_wishlist("7656119000000000")
    assert [(e.app_id, e.priority) for e in entries] == [(7, 12), (8, 0)]
    assert entries[0].added_at == datetime(2020, 1, 1) and entries[1].added_at == datetime(2020, 1, 2)


def test_only_the_steam_id_is_sent_never_a_key(http):
    fetch_wishlist("7656119000000000")
    (url, params), = http.calls
    assert url.endswith("IWishlistService/GetWishlist/v1/") and params == {"steamid": "7656119000000000"}


@pytest.mark.parametrize("body", [{"response": {}}, {"response": {"items": []}}, {"response": None}, {}])
def test_an_empty_or_private_wishlist_is_an_empty_list(http, body):
    http.answer["response"] = FakeResponse(200, body)
    assert fetch_wishlist("id") == []


def test_an_item_without_a_date_or_a_rank_is_still_read(http):
    http.answer["response"] = FakeResponse(200, {"response": {"items": [{"appid": 7}, {"nonsense": 1}]}})
    (only,) = fetch_wishlist("id")
    assert (only.app_id, only.priority, only.added_at) == (7, None, None)


@pytest.mark.parametrize("status", [403, 429])
def test_a_rate_limit_is_raised_as_such(http, status):
    http.answer["response"] = FakeResponse(status, {})
    with pytest.raises(RateLimited):
        fetch_wishlist("id")


def test_a_server_error_is_raised(http):
    http.answer["response"] = FakeResponse(500, {})
    with pytest.raises(requests.HTTPError):
        fetch_wishlist("id")


# ---- keeping it in step ----


def test_unknown_games_are_added_as_bare_rows_to_fetch_details_for_later(session):
    result = import_wishlist(session, [entry(7, 3, datetime(2020, 1, 1)), entry(8)])
    assert (result.total, result.added, result.removed) == (2, 2, 0)
    assert session.get(Game, 7).name == "App 7" and session.get(Game, 7).details_fetched_at is None
    item = session.get(WishlistItem, 7)
    assert (item.priority, item.added_at) == (3, datetime(2020, 1, 1)) and item.seen_at is not None


def test_a_game_already_known_keeps_everything_it_had(session):
    add_game(session, 7, "Real Name", tags={"Stealth": 10}, owned=False)
    import_wishlist(session, [entry(7, 1)])
    game = session.get(Game, 7)
    assert game.name == "Real Name" and game.tags == {"Stealth": 10}
    assert session.query(Game).filter(Game.steam_app_id == 7).count() == 1


def test_importing_again_updates_ranks_and_adds_no_duplicates(session):
    import_wishlist(session, [entry(7, 5), entry(8, 0)])
    second = import_wishlist(session, [entry(7, 2), entry(8, 9), entry(9)])
    assert (second.total, second.added, second.removed) == (3, 1, 0)
    assert session.query(WishlistItem).count() == 3
    assert (session.get(WishlistItem, 7).priority, session.get(WishlistItem, 8).priority) == (2, 9)


def test_a_game_no_longer_listed_leaves_the_wishlist_but_stays_in_the_database(session):
    add_game(session, 8, "Bought it", owned=False)
    import_wishlist(session, [entry(7), entry(8)])
    result = import_wishlist(session, [entry(7)])
    assert (result.total, result.added, result.removed) == (1, 0, 1)
    assert session.get(WishlistItem, 8) is None and session.get(Game, 8) is not None


def test_an_empty_answer_keeps_the_earlier_wishlist_since_it_probably_means_private(session):
    import_wishlist(session, [entry(7), entry(8)])
    result = import_wishlist(session, [])
    assert result.kept_old_list and result.total == 2 and result.removed == 0
    assert session.query(WishlistItem).count() == 2


def test_an_empty_answer_with_nothing_saved_is_just_an_empty_wishlist(session):
    result = import_wishlist(session, [])
    assert (result.total, result.added, result.removed, result.kept_old_list) == (0, 0, 0, False)


def test_importing_never_touches_your_library_or_diary(session):
    add_game(session, 7, "Owned", minutes=600)  # in the library
    import_wishlist(session, [entry(7)])
    game = session.get(Game, 7)
    assert game.library_entry is not None and game.library_entry.playtime_total_min == 600


# ---- putting it in order ----


def test_a_rank_is_shown_only_for_games_you_ranked():
    assert rank_label(12) == "#12" and rank_label(0) is None and rank_label(None) is None


def test_your_order_is_ranked_games_first_then_the_unranked_newest_first(session):
    import_wishlist(
        session,
        [
            entry(1, 0, datetime(2020, 1, 1)),  # unranked, old
            entry(2, 5, datetime(2019, 1, 1)),
            entry(3, 0, datetime(2024, 1, 1)),  # unranked, new
            entry(4, 1, datetime(2025, 1, 1)),
            entry(5, None, None),  # unranked, no date
        ],
    )
    items = session.query(WishlistItem).all()
    assert [i.steam_app_id for i in sorted(items, key=order_key)] == [4, 2, 3, 1, 5]


# ---- the command-line script ----


def test_the_script_imports_the_wishlist_and_says_what_changed(session, monkeypatch, capsys):
    from scripts import wishlist as script
    from shelflife.config import Settings

    monkeypatch.setattr(script, "load_settings", lambda: Settings(steam_api_key="k", steam_id="76561190000000000"))
    seen = []
    monkeypatch.setattr(script, "fetch_wishlist", lambda steam_id: seen.append(steam_id) or [entry(7, 1), entry(8)])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert seen == ["76561190000000000"] and "2 games on your wishlist: 2 new, 0 no longer on it" in out
    assert "scripts.sync" in out and session.query(WishlistItem).count() == 2


def test_the_script_keeps_the_old_list_when_steam_answers_with_nothing(session, monkeypatch, capsys):
    from scripts import wishlist as script
    from shelflife.config import Settings

    import_wishlist(session, [entry(7)])
    monkeypatch.setattr(script, "load_settings", lambda: Settings(steam_api_key="k", steam_id="i"))
    monkeypatch.setattr(script, "fetch_wishlist", lambda steam_id: [])
    assert script.main() == 0
    assert "earlier one was kept" in capsys.readouterr().out and session.query(WishlistItem).count() == 1


@pytest.mark.parametrize("failure, code", [(RateLimited("slow"), 2), (requests.ConnectionError("down"), 1), (ValueError("bad json"), 1)])
def test_the_script_reports_failures_with_a_status_code(session, monkeypatch, capsys, failure, code):
    from scripts import wishlist as script
    from shelflife.config import Settings

    monkeypatch.setattr(script, "load_settings", lambda: Settings(steam_api_key="k", steam_id="i"))
    monkeypatch.setattr(script, "fetch_wishlist", lambda steam_id: (_ for _ in ()).throw(failure))
    assert script.main() == code and capsys.readouterr().err
