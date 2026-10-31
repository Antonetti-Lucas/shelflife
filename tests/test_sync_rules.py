"""What a sync re-reads: achievements, store details and review counts, and when."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from shelflife import store, sync
from shelflife.achievements import AchievementStats
from shelflife.models import Game, LibraryEntry
from shelflife.steam import SteamError
from shelflife.sync import (
    DETAILS_REFRESH_BATCH,
    achievements_due,
    details_due,
    enrich_games,
    enrich_one,
    reviews_due,
    sync_achievements,
    sync_all_reviews,
)
from shelflife.throttle import RateLimited
from tests.factories import add_game


def ago(days):
    """A time that many days ago (the code compares with UTC clock time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


def ids(rows):
    return [r.steam_app_id for r in rows]


def set_fetched(session, app_id, achievements=None, details=None, reviews=None, last_played=None):
    entry, game = session.get(LibraryEntry, app_id), session.get(Game, app_id)
    if achievements is not None and entry is not None:
        entry.achievements_fetched_at = achievements
        entry.achievements_unlocked = entry.achievements_unlocked or 1
        entry.unlock_times = entry.unlock_times or [100]
    if details is not None:
        game.details_fetched_at = details
    if reviews is not None:
        game.reviews_fetched_at = reviews
    if last_played is not None:
        entry.last_played = last_played
    session.commit()


# ---- achievements ----


def test_a_game_never_read_is_due(session):
    add_game(session, 1, minutes=600)
    assert ids(achievements_due(session)) == [1]


def test_a_game_read_recently_and_not_played_since_is_not_due(session):
    add_game(session, 1, minutes=600, achievements=(3, 10), unlock_times=[1])
    set_fetched(session, 1, achievements=ago(2), last_played=ago(10))
    assert achievements_due(session) == []


def test_a_game_played_since_it_was_last_read_is_due_even_if_not_in_the_last_two_weeks(session):
    # Regression: only games played in the last two weeks were re-read, so a game played three
    # weeks ago (after the last sync) never got its new achievements.
    add_game(session, 1, minutes=600, achievements=(3, 10), unlock_times=[1], recent=0)
    set_fetched(session, 1, achievements=ago(25), last_played=ago(21))
    assert ids(achievements_due(session)) == [1]


def test_once_read_again_a_game_is_not_due_again(session, monkeypatch):
    add_game(session, 1, minutes=600, achievements=(3, 10), unlock_times=[1])
    set_fetched(session, 1, achievements=ago(25), last_played=ago(21))
    stats = AchievementStats(10, 4, 5.0, ago(30), ago(21), (100, 200))
    monkeypatch.setattr(sync, "fetch_achievements", lambda key, sid, app_id: stats)
    assert sync_achievements(session, "k", "i") == 1
    assert achievements_due(session) == []


def test_a_game_not_read_for_a_month_is_due_because_developers_add_achievements(session):
    add_game(session, 1, minutes=600, achievements=(3, 10), unlock_times=[1])
    add_game(session, 2, minutes=600, achievements=(3, 10), unlock_times=[1])
    set_fetched(session, 1, achievements=ago(31), last_played=ago(100))
    set_fetched(session, 2, achievements=ago(29), last_played=ago(100))
    assert ids(achievements_due(session)) == [1]


def test_family_games_are_re_read_by_age_since_they_show_no_playtime(session):
    add_game(session, 1, owned=False, family=True, achievements=(1, 5), unlock_times=[1])
    add_game(session, 2, owned=False, family=True, achievements=(1, 5), unlock_times=[1])
    set_fetched(session, 1, achievements=ago(40))
    set_fetched(session, 2, achievements=ago(3))
    assert ids(achievements_due(session)) == [1]


def test_everything_re_reads_all_played_and_family_games_but_never_unplayed_owned_ones(session):
    add_game(session, 1, minutes=600, achievements=(3, 10), unlock_times=[1])
    add_game(session, 2, owned=False, family=True, achievements=(1, 5), unlock_times=[1])
    add_game(session, 3, minutes=0)  # owned, never played
    set_fetched(session, 1, achievements=ago(1), last_played=ago(50))
    set_fetched(session, 2, achievements=ago(1))
    assert achievements_due(session) == []
    assert sorted(ids(achievements_due(session, everything=True))) == [1, 2]


def test_the_most_played_games_come_first(session):
    for app_id, minutes in ((1, 100), (2, 5000), (3, 900)):
        add_game(session, app_id, minutes=minutes)
    assert ids(achievements_due(session)) == [2, 3, 1]


# ---- review counts ----


def test_a_played_game_without_review_counts_is_due(session):
    # Regression: only barely-played games got counts, so 201 of 206 played games had none.
    add_game(session, 1, minutes=6000)
    add_game(session, 2, minutes=0)
    add_game(session, 3, owned=False, family=True)
    assert sorted(ids(reviews_due(session))) == [1, 2, 3]


def test_things_that_are_not_games_and_games_you_do_not_have_are_not_due(session):
    add_game(session, 1, minutes=60, type="dlc")
    add_game(session, 2, owned=False)  # in the pool, not in your library
    assert reviews_due(session) == []


def test_counts_are_refreshed_monthly_oldest_first_after_the_missing_ones(session):
    for app_id in (1, 2, 3, 4):
        add_game(session, app_id, minutes=60)
    set_fetched(session, 1, reviews=ago(40))
    set_fetched(session, 2, reviews=ago(10))  # fresh
    set_fetched(session, 3, reviews=ago(90))
    # game 4 has no counts at all
    assert ids(reviews_due(session)) == [4, 3, 1]


def test_everything_reads_all_the_counts_again(session):
    add_game(session, 1, minutes=60)
    set_fetched(session, 1, reviews=ago(1))
    assert reviews_due(session) == [] and ids(reviews_due(session, everything=True)) == [1]


def test_counts_are_stored_with_their_date(session, monkeypatch):
    add_game(session, 1, minutes=6000)
    monkeypatch.setattr(sync, "fetch_review_counts", lambda app_id: SimpleNamespace(positive=950, negative=50))
    assert sync_all_reviews(session) == 1
    game = session.get(Game, 1)
    assert (game.reviews_positive, game.reviews_negative) == (950, 50)
    assert game.reviews_fetched_at is not None and reviews_due(session) == []


def test_a_game_with_no_reviews_at_all_is_marked_read_so_it_is_not_asked_every_sync(session, monkeypatch):
    add_game(session, 1, minutes=60)
    monkeypatch.setattr(sync, "fetch_review_counts", lambda app_id: None)
    sync_all_reviews(session)
    game = session.get(Game, 1)
    assert game.reviews_positive is None and game.reviews_fetched_at is not None
    assert reviews_due(session) == []


def test_one_failed_review_lookup_is_skipped_but_three_in_a_row_stop_it(session, monkeypatch):
    for app_id in range(1, 6):
        add_game(session, app_id, minutes=60)
    answers = iter([requests.ConnectionError("blip")] + [SimpleNamespace(positive=9, negative=1)] * 10)

    def fetch(app_id):
        outcome = next(answers)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sync, "fetch_review_counts", fetch)
    assert sync_all_reviews(session) == 4  # one skipped, four read

    monkeypatch.setattr(sync, "fetch_review_counts", lambda app_id: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(SteamError, match="3 failures in a row"):
        sync_all_reviews(session, everything=True)


def test_rate_limiting_while_reading_counts_is_raised(session, monkeypatch):
    add_game(session, 1, minutes=60)
    monkeypatch.setattr(sync, "fetch_review_counts", lambda app_id: (_ for _ in ()).throw(RateLimited("slow down")))
    with pytest.raises(RateLimited):
        sync_all_reviews(session)


def test_reading_counts_can_be_stopped_between_games(session, monkeypatch):
    for app_id in (1, 2, 3):
        add_game(session, app_id, minutes=60)
    monkeypatch.setattr(sync, "fetch_review_counts", lambda app_id: SimpleNamespace(positive=9, negative=1))
    done = []
    assert sync_all_reviews(session, on_progress=lambda *a: done.append(1), should_stop=lambda: len(done) >= 2) == 2


# ---- store details ----


def test_new_games_come_first_most_played_first(session):
    add_game(session, 1, minutes=10)
    add_game(session, 2, minutes=900)
    add_game(session, 3, minutes=10)
    set_fetched(session, 3, details=ago(1))
    assert ids(details_due(session)) == [2, 1]


def test_a_game_the_store_had_no_page_for_is_retried_after_a_week_not_before(session):
    add_game(session, 1, minutes=60, type="unavailable")
    add_game(session, 2, minutes=60, type="unavailable")
    set_fetched(session, 1, details=ago(8))
    set_fetched(session, 2, details=ago(3))
    assert ids(details_due(session)) == [1]


def test_a_game_that_came_back_with_no_tags_is_retried_after_a_week(session):
    add_game(session, 1, minutes=60, tags={})
    add_game(session, 2, minutes=60, tags={"Stealth": 10})
    set_fetched(session, 1, details=ago(8))
    set_fetched(session, 2, details=ago(8))
    assert ids(details_due(session)) == [1]


def test_old_details_are_refreshed_oldest_first_but_only_a_batch_at_a_time(session):
    total = DETAILS_REFRESH_BATCH + 5
    for app_id in range(1, total + 1):
        add_game(session, app_id, minutes=60, tags={"Stealth": 10})
        set_fetched(session, app_id, details=ago(100 + app_id))  # game 35 is the oldest
    due = ids(details_due(session))
    assert len(due) == DETAILS_REFRESH_BATCH
    assert due[0] == total and due == sorted(due, reverse=True)  # the oldest first
    assert 1 not in due  # the five youngest wait for a later sync


def test_details_under_ninety_days_old_are_left_alone(session):
    add_game(session, 1, minutes=60, tags={"Stealth": 10})
    set_fetched(session, 1, details=ago(60))
    assert details_due(session) == []


def test_a_game_both_empty_and_old_is_listed_once(session):
    add_game(session, 1, minutes=60, tags={})
    set_fetched(session, 1, details=ago(200))
    assert ids(details_due(session)) == [1]


def test_the_order_is_new_then_retries_then_old_ones(session):
    add_game(session, 1, minutes=60, tags={"A": 1})
    set_fetched(session, 1, details=ago(150))  # old
    add_game(session, 2, minutes=60, tags={}, type="unavailable")
    set_fetched(session, 2, details=ago(20))  # retry
    add_game(session, 3, minutes=60)  # new
    assert ids(details_due(session)) == [3, 2, 1]


def test_games_outside_your_library_are_never_due_for_details(session):
    add_game(session, 1, owned=False, tags={})
    set_fetched(session, 1, details=ago(300))
    assert details_due(session) == []


def test_everything_lists_every_library_game(session):
    add_game(session, 1, minutes=60, tags={"A": 1})
    set_fetched(session, 1, details=ago(1))
    add_game(session, 2, owned=False)
    assert details_due(session) == [] and ids(details_due(session, everything=True)) == [1]


@pytest.fixture
def fake_store(monkeypatch):
    """A store that answers for the games in `pages` (app id -> (type, name)) and not for others."""
    pages = {}
    fetched = []

    def details(app_id, timeout=15):
        fetched.append(app_id)
        if app_id not in pages:
            return None
        kind, name = pages[app_id]
        return store.StoreDetails(type=kind, short_description="d", genres=[], release_date=None, cover_url=None, name=name)

    monkeypatch.setattr(store, "fetch_store_details", details)
    monkeypatch.setattr(sync, "_fill_tags", lambda game, spy=None: None)
    details.pages, details.fetched = pages, fetched
    return details


def test_a_refresh_that_hits_a_store_hiccup_does_not_turn_a_good_game_unavailable(session, fake_store):
    add_game(session, 1, minutes=60, type="game")  # fine before; the store now answers with no page
    enrich_one(session.get(Game, 1))
    assert session.get(Game, 1).type == "game"


def test_a_game_the_store_has_never_answered_for_becomes_unavailable(session, fake_store):
    game = add_game(session, 1, minutes=60, type=None)
    enrich_one(game)
    assert session.get(Game, 1).type == "unavailable"


def test_a_game_that_was_unavailable_becomes_a_game_when_the_store_gets_a_page(session, fake_store):
    add_game(session, 1, minutes=60, type="unavailable")
    fake_store.pages[1] = ("game", "Now on sale")
    enrich_one(session.get(Game, 1))
    assert session.get(Game, 1).type == "game"


def test_without_refresh_only_new_games_are_enriched_as_before(session, fake_store):
    add_game(session, 1, minutes=60, type="unavailable")
    set_fetched(session, 1, details=ago(30))
    add_game(session, 2, minutes=60)
    fake_store.pages[2] = ("game", "Two")
    assert enrich_games(session) == 1 and fake_store.fetched == [2]


def test_with_refresh_the_retries_are_enriched_too(session, fake_store):
    add_game(session, 1, minutes=60, type="unavailable")
    set_fetched(session, 1, details=ago(30))
    add_game(session, 2, minutes=60)
    fake_store.pages[1], fake_store.pages[2] = ("game", "One"), ("game", "Two")
    assert enrich_games(session, refresh=True) == 2 and sorted(fake_store.fetched) == [1, 2]
    assert session.get(Game, 1).type == "game"


def test_a_refresh_can_be_stopped_between_games(session, fake_store):
    for app_id in (1, 2, 3):
        add_game(session, app_id, minutes=60)
        fake_store.pages[app_id] = ("game", f"G{app_id}")
    done = []
    assert enrich_games(session, refresh=True, on_progress=lambda *a: done.append(1), should_stop=lambda: len(done) >= 1) == 1


# ---- games played lately ----


def test_a_game_played_in_the_last_two_weeks_is_read_again_but_not_right_after_it_was_read(session):
    # Regression: such a game was due on every sync, so the page still said "due" right after a sync.
    add_game(session, 1, minutes=600, recent=30, achievements=(3, 10), unlock_times=[1])
    add_game(session, 2, minutes=600, recent=30, achievements=(3, 10), unlock_times=[1])
    set_fetched(session, 1, achievements=ago(0.04), last_played=ago(1))  # read an hour ago
    set_fetched(session, 2, achievements=ago(0.5), last_played=ago(1))  # read twelve hours ago
    assert ids(achievements_due(session)) == [2]


# ---- wishlist games are wanted too ----


def wish(session, *app_ids):
    from shelflife.wishlist import WishlistEntry, import_wishlist

    import_wishlist(session, [WishlistEntry(i, 0, None) for i in app_ids])


def test_new_wishlist_games_are_due_for_details_after_the_library_games(session):
    add_game(session, 1, minutes=100)
    wish(session, 50, 51)
    due = ids(details_due(session))
    assert due[0] == 1 and set(due) == {1, 50, 51}  # your library first, then the wishlist


def test_wishlist_games_are_due_for_review_counts_once_their_type_is_known(session):
    wish(session, 50)
    assert reviews_due(session) == []  # type unknown until the store details arrive
    session.get(Game, 50).type = "game"
    session.commit()
    assert ids(reviews_due(session)) == [50]


def test_a_game_neither_in_the_library_nor_on_the_wishlist_is_still_never_due(session):
    add_game(session, 1, owned=False)  # discovery pool
    wish(session, 50)
    assert 1 not in ids(details_due(session)) and 1 not in ids(reviews_due(session))


def test_a_wishlist_game_that_came_back_empty_is_retried_like_any_other(session):
    wish(session, 50)
    game = session.get(Game, 50)
    game.type, game.details_fetched_at = "unavailable", ago(10)
    session.commit()
    assert ids(details_due(session)) == [50]


def test_the_command_line_default_also_fetches_wishlist_games(session, monkeypatch):
    wish(session, 50)
    fetched = []
    monkeypatch.setattr(sync, "enrich_one", lambda game, spy=None: fetched.append(game.steam_app_id) or setattr(game, "details_fetched_at", ago(0)))
    assert enrich_games(session) == 1 and fetched == [50]


def test_wishlist_games_never_get_achievements_read(session):
    wish(session, 50)
    assert achievements_due(session) == []
