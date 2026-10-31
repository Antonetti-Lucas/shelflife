"""The wishlist in the app: its page, Explore, the home page and game pages."""

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.models import Game
from shelflife.recommend import recommend, taste_profile
from shelflife.web.app import app
from shelflife.wishlist import WishlistEntry, import_wishlist
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def scenario(session):
    ratings = build_scenario(session)
    for app_id, rating in ratings.items():
        diary.save_rating(session, app_id, rating)
    return ratings


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def titles(html):
    """The game cards' titles, in the order shown."""
    return re.findall(r'<h3><a href="/game/\d+">([^<]+)</a></h3>', html)


def wish(session, *entries):
    import_wishlist(session, [e if isinstance(e, WishlistEntry) else WishlistEntry(e, 0, None) for e in entries])


# ---- the page ----


def test_an_empty_wishlist_says_how_to_fill_it(client):
    html = client.get("/wishlist").text
    assert "Nothing here yet" in html and 'href="/sync"' in html


def test_the_page_lists_your_wishlisted_games_best_fit_first_and_nothing_else(client, session, scenario):
    wish(session, 40, 41)
    html = client.get("/wishlist").text
    assert titles(html) == ["Pool Stealth Good", "Pool Stealth Bad"]
    assert "2 of 2 games on your Steam wishlist" in html
    assert "Backlog Stealth" not in html and "Family Stealth" not in html


def test_each_card_says_your_rank_and_when_you_added_it(client, session, scenario):
    wish(session, WishlistEntry(40, 12, datetime(2024, 5, 1)), WishlistEntry(41, 0, datetime(2023, 2, 3)))
    html = client.get("/wishlist").text
    assert "On your wishlist · your rank #12 · added 2024-05-01" in html
    assert "On your wishlist · added 2023-02-03" in html  # no rank, so none is shown


def test_the_sorts_follow_your_own_order_the_date_the_reviews_and_the_name(client, session, scenario):
    add_game(session, 142, "Aardvark", tags={"Stealth": 100}, owned=False, reviews=(100, 100))
    wish(session, WishlistEntry(40, 7, datetime(2020, 1, 1)), WishlistEntry(41, 2, datetime(2023, 1, 1)), WishlistEntry(142, 0, datetime(2025, 1, 1)))
    order = lambda sort: titles(client.get(f"/wishlist?sort={sort}").text)  # noqa: E731
    assert order("priority") == ["Pool Stealth Bad", "Pool Stealth Good", "Aardvark"]  # ranked 2, 7, then unranked
    assert order("added") == ["Aardvark", "Pool Stealth Bad", "Pool Stealth Good"]
    assert order("name") == ["Aardvark", "Pool Stealth Bad", "Pool Stealth Good"]
    assert order("reviews") == ["Pool Stealth Good", "Pool Stealth Bad", "Aardvark"]
    assert order("fit")[0] == "Pool Stealth Good"
    assert order("nonsense") == order("fit")


def test_search_tags_and_the_best_fit_cut_work_on_the_wishlist(client, session, scenario):
    add_game(session, 142, "Racing Sim", tags={"Racing": 100}, owned=False, reviews=(900, 100))
    wish(session, 40, 41, 142)
    assert titles(client.get("/wishlist?q=racing").text) == ["Racing Sim"]
    assert set(titles(client.get("/wishlist?tag=Stealth").text)) == {"Pool Stealth Good", "Pool Stealth Bad"}
    assert titles(client.get("/wishlist?tag=Racing,Stealth&match=any").text).count("Racing Sim") == 1
    assert titles(client.get("/wishlist?rec=10").text) == ["Pool Stealth Good"]  # the top tenth of the wishlist


def test_games_still_waiting_for_details_and_ones_that_cannot_be_ranked_are_counted_not_hidden(client, session, scenario):
    wish(session, 40, 900)  # 900 is unknown: only a bare row
    add_game(session, 43, "A DLC", type="dlc", owned=False)
    session.get(Game, 43).details_fetched_at = datetime(2026, 1, 1)
    session.commit()
    wish(session, 40, 900, 43)
    html = client.get("/wishlist").text
    assert "1 wishlist game still waiting for store details" in html
    assert "1 can't be ranked (DLC or software, or no tags)" in html
    assert titles(html) == ["Pool Stealth Good"] and "App 900" not in html


def test_a_game_you_marked_not_interested_is_hidden_and_counted(client, session, scenario):
    wish(session, 40, 41)
    post(client, "/game/41/not-interested", value="1")
    html = client.get("/wishlist").text
    assert titles(html) == ["Pool Stealth Good"] and '1 you marked "not interested" is hidden' in html


def test_a_family_game_on_the_wishlist_says_you_can_already_play_it(client, session, scenario):
    wish(session, 30)  # Family Stealth
    assert "you can already play it with Family Sharing" in client.get("/wishlist").text


def test_a_rating_you_gave_a_wishlisted_game_shows_on_its_card(client, session, scenario):
    wish(session, 40)
    post(client, "/game/40/rate", rating="3.5")
    assert re.search(r'<option value="3.5" selected', client.get("/wishlist").text)


def test_the_wishlist_pages_keep_your_filters(client, session):
    for i in range(100, 160):
        add_game(session, i, f"Game {i}", tags={"Stealth": 100}, owned=False, reviews=(900, 100))
    wish(session, *range(100, 160))
    html = client.get("/wishlist?tag=Stealth&sort=name").text
    pager = re.search(r'class="pager".*?</p>', html, re.S).group(0)
    assert "tag=Stealth" in pager and "sort=name" in pager and "page=2" in pager
    assert len(titles(client.get("/wishlist?page=3").text)) == 60 - 48 and client.get("/wishlist?page=99").status_code == 200


# ---- the rest of the app ----


def test_wishlisting_a_game_never_changes_your_taste(session, scenario):
    before = taste_profile(session, scenario, {}).profile
    wish(session, 40, 41, 30)
    assert taste_profile(session, scenario, {}).profile == before


def test_explore_marks_wishlisted_games_and_can_show_only_those(client, session, scenario):
    wish(session, WishlistEntry(40, 4, datetime(2024, 5, 1)))
    everything = client.get("/explore").text
    assert "★ On your wishlist · your rank #4 · added 2024-05-01" in everything and "Pool Stealth Bad" in everything
    only = client.get("/explore?wishlist=only").text
    assert titles(only) == ["Pool Stealth Good"] and 'value="only" selected' in only
    assert client.get("/explore?wishlist=banana").text.count("Pool Stealth Bad") >= 1  # anything else means any game


def test_the_home_page_has_a_wishlist_list_and_each_game_is_listed_once(client, session, scenario):
    wish(session, 40)
    html = client.get("/").text
    wishlist_part = html[html.index('id="wishlist"') : html.index('id="discover"')]
    discover_part = html[html.index('id="discover"') :]
    assert "Pool Stealth Good" in wishlist_part and "Pool Stealth Good" not in discover_part
    assert "Pool Stealth Bad" in discover_part


def test_the_home_page_wishlist_list_says_what_to_do_when_empty(client, scenario):
    html = client.get("/").text
    assert "From your wishlist" in html and "Nothing from your wishlist yet" in html


def test_the_recommender_lists_only_unowned_wishlist_games(session, scenario):
    wish(session, 40, 20, 30)  # 20 is owned (backlog), 30 is family
    result = recommend(session, scenario, limit=50, wishlist_ids={40, 20, 30})
    assert [r.game.steam_app_id for r in result.wishlist] == [40]
    assert 40 not in [r.game.steam_app_id for r in result.discover]
    assert 20 in [r.game.steam_app_id for r in result.backlog] and 30 in [r.game.steam_app_id for r in result.family]


def test_without_a_wishlist_the_discover_list_is_unchanged(session, scenario):
    result = recommend(session, scenario, limit=50)
    assert result.wishlist == [] and {r.game.steam_app_id for r in result.discover} >= {40, 41}


def test_a_game_page_says_when_the_game_is_on_your_wishlist(client, session, scenario):
    wish(session, WishlistEntry(40, 9, datetime(2022, 8, 9)))
    assert "★ On your wishlist · your rank #9 · added 2022-08-09" in client.get("/game/40").text
    assert "On your wishlist" not in client.get("/game/41").text
