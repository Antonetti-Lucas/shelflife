"""The diary: ratings, reviews and "not interested" marks."""

from pathlib import Path

import pytest

from shelflife import diary
from shelflife.models import DiaryEntry
from tests.factories import add_game


@pytest.fixture
def game(session):
    return add_game(session, 7, "Some Game")


def test_a_rating_is_saved(session, game):
    diary.save_rating(session, 7, 4.5)
    assert diary.load_ratings(session) == {7: 4.5}


@pytest.mark.parametrize("bad", [0, 0.25, 4.3, 5.5, -1, 10])
def test_ratings_must_be_half_steps_from_half_a_star_to_five(session, game, bad):
    with pytest.raises(ValueError):
        diary.save_rating(session, 7, bad)


def test_rating_an_unknown_game_is_an_error(session):
    with pytest.raises(ValueError):
        diary.save_rating(session, 12345, 4.0)


def test_clearing_the_rating_removes_the_entry(session, game):
    diary.save_rating(session, 7, 4.0)
    diary.save_rating(session, 7, None)
    assert diary.get_entry(session, 7) is None


def test_clearing_the_rating_of_a_game_with_no_entry_does_nothing(session, game):
    # Regression: this used to crash while trying to delete an entry that was
    # never saved (blank "Rate…" choice on a recommendation card).
    diary.save_rating(session, 7, None)
    assert diary.get_entry(session, 7) is None
    assert session.query(DiaryEntry).count() == 0


def test_a_review_survives_a_rating_change_when_not_mentioned(session, game):
    diary.save_rating(session, 7, 4.0, "great")
    diary.save_rating(session, 7, 3.0)  # review not passed: leave it
    entry = diary.get_entry(session, 7)
    assert (entry.rating, entry.review) == (3.0, "great")


def test_an_empty_review_clears_it(session, game):
    diary.save_rating(session, 7, 4.0, "great")
    diary.save_rating(session, 7, 4.0, "")
    assert diary.get_entry(session, 7).review is None


def test_a_review_of_only_spaces_counts_as_empty(session, game):
    diary.save_rating(session, 7, 4.0, "   ")
    assert diary.get_entry(session, 7).review is None


def test_a_review_without_a_rating_is_kept(session, game):
    diary.save_rating(session, 7, None, "just a note")
    assert diary.get_entry(session, 7).review == "just a note"
    assert diary.load_ratings(session) == {}


def test_removing_both_rating_and_review_deletes_the_entry(session, game):
    diary.save_rating(session, 7, 4.0, "great")
    diary.save_rating(session, 7, None, "")
    assert diary.get_entry(session, 7) is None


def test_not_interested_is_marked_and_listed_by_name(session, game):
    diary.set_not_interested(session, 7, True)
    assert diary.load_not_interested(session) == {7: "Some Game"}


def test_not_interested_can_be_undone(session, game):
    diary.set_not_interested(session, 7, True)
    diary.set_not_interested(session, 7, False)
    assert diary.load_not_interested(session) == {}
    assert diary.get_entry(session, 7) is None


def test_not_interested_does_not_count_as_a_rating(session, game):
    diary.set_not_interested(session, 7, True)
    assert diary.load_ratings(session) == {}


def test_a_rated_game_can_also_be_marked_not_interested_without_losing_the_rating(session, game):
    diary.save_rating(session, 7, 4.0)
    diary.set_not_interested(session, 7, True)
    diary.set_not_interested(session, 7, False)
    assert diary.load_ratings(session) == {7: 4.0}


# ---- importing the old CSV files ----


def write_csv(name, text):
    folder = Path("data")
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8-sig")


def test_legacy_csv_files_are_imported_once(session, game):
    add_game(session, 8, "Other Game")
    write_csv(
        "ratings.csv",
        "steam_app_id,name,hours,achievements,rating_0.5_to_5\n7,Some Game,10,,4.5\n8,Other Game,5,,\n",
    )
    write_csv("feedback.csv", "steam_app_id,name,verdict\n8,Other Game,not_interested\n")

    assert diary.import_legacy_csv(session) == (1, 1)
    assert diary.load_ratings(session) == {7: 4.5}
    assert diary.load_not_interested(session) == {8: "Other Game"}
    assert diary.import_legacy_csv(session) == (0, 0)  # safe to run again


def test_the_import_never_overwrites_what_is_already_in_the_diary(session, game):
    diary.save_rating(session, 7, 2.0)
    write_csv("ratings.csv", "steam_app_id,name,hours,achievements,rating_0.5_to_5\n7,Some Game,10,,5\n")
    diary.import_legacy_csv(session)
    assert diary.load_ratings(session) == {7: 2.0}


def test_the_import_skips_games_that_are_not_in_the_database(session, game):
    write_csv("ratings.csv", "steam_app_id,name,hours,achievements,rating_0.5_to_5\n999,Ghost,1,,5\n")
    assert diary.import_legacy_csv(session) == (0, 0)
