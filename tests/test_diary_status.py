"""The diary's status, dates and favorites."""

from datetime import date

import pytest
from sqlalchemy import inspect, text

from shelflife import diary
from shelflife.db import engine, init_db
from tests.factories import add_game

TODAY = date(2026, 7, 4)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(diary, "_today", lambda: TODAY)


@pytest.fixture
def game(session):
    return add_game(session, 7, "Some Game")


def test_a_status_is_saved(session, game):
    diary.set_status(session, 7, "playing")
    assert diary.get_entry(session, 7).status == "playing"


@pytest.mark.parametrize("bad", ["finished", "PLAYING", "", "wishlist"])
def test_only_known_statuses_are_accepted(session, game, bad):
    with pytest.raises(ValueError):
        diary.set_status(session, 7, bad)
    assert diary.get_entry(session, 7) is None


def test_a_status_for_an_unknown_game_is_an_error(session):
    with pytest.raises(ValueError):
        diary.set_status(session, 12345, "played")


# ---- dates ----


def test_playing_fills_in_todays_start_date(session, game):
    diary.set_status(session, 7, "playing")
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (TODAY, None)


@pytest.mark.parametrize("status", ["played", "dropped"])
def test_finishing_or_dropping_fills_in_todays_finish_date(session, game, status):
    diary.set_status(session, 7, status)
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (None, TODAY)


def test_dates_you_give_are_kept(session, game):
    diary.set_status(session, 7, "played", date(2025, 1, 2), date(2025, 2, 3))
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (date(2025, 1, 2), date(2025, 2, 3))


def test_moving_from_playing_to_played_keeps_the_start_and_adds_the_finish(session, game):
    diary.set_status(session, 7, "playing", date(2026, 6, 1))
    diary.set_status(session, 7, "played")
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (date(2026, 6, 1), TODAY)


def test_leaving_the_dates_out_keeps_them(session, game):
    diary.set_status(session, 7, "played", date(2025, 1, 2), date(2025, 2, 3))
    diary.set_status(session, 7, "dropped")  # the quick picker in the library: status only
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (date(2025, 1, 2), date(2025, 2, 3))


def test_a_date_can_be_cleared_when_the_status_stays_the_same(session, game):
    diary.set_status(session, 7, "played")
    diary.set_status(session, 7, "played", None, None)  # you don't know when
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (None, None)


def test_an_unknown_date_given_with_a_new_status_is_not_replaced_by_today(session, game):
    # Logging an old game: you pick "played" and leave the date empty.
    diary.set_status(session, 7, "played", None, None)
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (None, None)
    assert entry.status == "played"


def test_giving_only_the_start_date_still_fills_in_the_finish_date_for_played(session, game):
    diary.set_status(session, 7, "played", date(2026, 6, 1))
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (date(2026, 6, 1), TODAY)


def test_finishing_before_starting_is_refused_and_changes_nothing(session, game):
    diary.set_status(session, 7, "played", date(2025, 1, 2), date(2025, 2, 3))
    with pytest.raises(ValueError):
        diary.set_status(session, 7, "played", date(2025, 5, 1), date(2025, 2, 3))
    entry = diary.get_entry(session, 7)
    assert (entry.started_on, entry.finished_on) == (date(2025, 1, 2), date(2025, 2, 3))


def test_a_refused_status_on_a_new_game_leaves_no_entry(session, game):
    with pytest.raises(ValueError):
        diary.set_status(session, 7, "played", date(2025, 5, 1), date(2025, 2, 3))
    assert diary.get_entry(session, 7) is None


def test_clearing_the_status_clears_the_dates_and_removes_an_otherwise_empty_entry(session, game):
    diary.set_status(session, 7, "played", date(2025, 1, 2), date(2025, 2, 3))
    diary.set_status(session, 7, None)
    assert diary.get_entry(session, 7) is None


def test_clearing_the_status_keeps_a_rating(session, game):
    diary.save_rating(session, 7, 4.0)
    diary.set_status(session, 7, "played")
    diary.set_status(session, 7, None)
    entry = diary.get_entry(session, 7)
    assert (entry.rating, entry.status, entry.finished_on) == (4.0, None, None)


# ---- the status and the rest of the entry do not clobber each other ----


def test_a_status_survives_clearing_the_rating(session, game):
    # Regression guard: the entry used to be deleted as soon as the rating was cleared.
    diary.save_rating(session, 7, 4.0)
    diary.set_status(session, 7, "playing")
    diary.save_rating(session, 7, None)
    assert diary.get_entry(session, 7).status == "playing"


def test_a_rating_survives_a_status_change(session, game):
    diary.save_rating(session, 7, 4.0, "good")
    diary.set_status(session, 7, "dropped")
    entry = diary.get_entry(session, 7)
    assert (entry.rating, entry.review) == (4.0, "good")


# ---- favorites ----


def test_a_favorite_is_saved_and_removed(session, game):
    diary.set_favorite(session, 7, True)
    assert diary.get_entry(session, 7).favorite is True
    diary.set_favorite(session, 7, False)
    assert diary.get_entry(session, 7) is None  # nothing left in it


def test_a_favorite_survives_clearing_the_rating_and_the_status(session, game):
    diary.save_rating(session, 7, 5.0)
    diary.set_status(session, 7, "played")
    diary.set_favorite(session, 7, True)
    diary.save_rating(session, 7, None)
    diary.set_status(session, 7, None)
    assert diary.get_entry(session, 7).favorite is True


def test_favoriting_an_unknown_game_is_an_error(session):
    with pytest.raises(ValueError):
        diary.set_favorite(session, 12345, True)


# ---- what a status says about taste ----


def test_a_dropped_game_stands_in_for_a_low_rating_and_a_favorite_for_a_top_one(session, game):
    add_game(session, 8, "Other Game")
    add_game(session, 9, "Plain Game")
    diary.set_status(session, 7, "dropped")
    diary.set_favorite(session, 8, True)
    diary.set_status(session, 9, "played")
    assert diary.implicit_ratings(session) == {7: diary.DROPPED_RATING, 8: diary.FAVORITE_RATING}


def test_a_rating_always_wins_over_a_status(session, game):
    diary.save_rating(session, 7, 4.5)
    diary.set_status(session, 7, "dropped")
    assert diary.implicit_ratings(session) == {}


def test_a_favorite_that_was_also_dropped_counts_as_a_favorite(session, game):
    diary.set_status(session, 7, "dropped")
    diary.set_favorite(session, 7, True)
    assert diary.implicit_ratings(session) == {7: diary.FAVORITE_RATING}


def test_load_entries_returns_every_entry_by_game(session, game):
    add_game(session, 8, "Other Game")
    diary.set_status(session, 7, "playing")
    diary.set_favorite(session, 8, True)
    assert set(diary.load_entries(session)) == {7, 8}


# ---- upgrading an existing database ----


def test_a_database_from_before_statuses_gains_the_new_columns_and_keeps_its_rows(session, game):
    diary.save_rating(session, 7, 4.0, "kept")
    new_columns = ("status", "started_on", "finished_on", "favorite")
    with engine.begin() as conn:
        for column in new_columns:
            conn.execute(text(f"ALTER TABLE diary_entries DROP COLUMN {column}"))
    assert not {c["name"] for c in inspect(engine).get_columns("diary_entries")} & set(new_columns)

    init_db()  # what the app does at startup

    assert set(new_columns) <= {c["name"] for c in inspect(engine).get_columns("diary_entries")}
    session.expire_all()
    entry = diary.get_entry(session, 7)
    assert (entry.rating, entry.review, entry.status, entry.favorite) == (4.0, "kept", None, None)
    diary.set_status(session, 7, "played")  # and the new columns are usable
    assert diary.get_entry(session, 7).status == "played"
