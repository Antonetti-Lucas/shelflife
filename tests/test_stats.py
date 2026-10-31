"""The numbers on the stats page."""

from datetime import date

from shelflife import diary
from shelflife.stats import compute_stats
from tests.factories import add_game, build_scenario


def test_an_empty_database_gives_empty_stats_without_errors(session):
    stats = compute_stats(session)
    assert (stats.owned, stats.family, stats.played, stats.backlog) == (0, 0, 0, 0)
    assert stats.hours_total == 0 and stats.top_hours == []
    assert stats.average_rating is None and stats.rated == 0
    assert stats.status_counts == {"playing": 0, "played": 0, "dropped": 0}
    assert stats.finished_by_year == [] and stats.finished_undated == 0
    assert set(stats.rating_counts) == {r / 2 for r in range(1, 11)}


def test_library_counts_cover_owned_family_played_and_backlog(session):
    build_scenario(session)  # 10 owned (2 big, 6 fillers, 2 backlog), 1 family, a pool of unowned games
    stats = compute_stats(session)
    assert (stats.owned, stats.family) == (10, 1)
    assert stats.played == 8  # the two big games and the six fillers
    assert stats.backlog == 2
    assert stats.hours_total == (3000 * 2 + 600 * 6) / 60  # unowned games add nothing


def test_a_family_game_counts_as_played_once_an_achievement_is_unlocked(session):
    add_game(session, 1, "Untouched", owned=False, family=True)
    add_game(session, 2, "Started", owned=False, family=True, achievements=(3, 10))
    stats = compute_stats(session)
    assert (stats.family, stats.played, stats.backlog) == (2, 1, 0)  # family games are never "backlog"


def test_dlc_and_software_are_not_counted_as_games(session):
    add_game(session, 1, "A Game", minutes=600)
    add_game(session, 2, "Some DLC", minutes=600, type="dlc")
    assert compute_stats(session).owned == 1


def test_most_played_is_sorted_and_capped_at_ten(session):
    for i in range(1, 13):
        add_game(session, i, f"Game {i}", minutes=i * 60)
    top = compute_stats(session).top_hours
    assert len(top) == 10
    assert [g.name for g, _ in top[:2]] == ["Game 12", "Game 11"]
    assert top[0][1] == 12.0


def test_ratings_are_counted_per_step_and_averaged(session):
    for i, rating in enumerate([5.0, 4.5, 4.5, 3.0], start=1):
        add_game(session, i, f"Game {i}")
        diary.save_rating(session, i, rating, "words" if i == 1 else None)
    stats = compute_stats(session)
    assert stats.rated == 4 and stats.average_rating == 4.25
    assert stats.rating_counts[4.5] == 2 and stats.rating_counts[5.0] == 1 and stats.rating_counts[1.0] == 0
    assert stats.reviews == 1


def test_a_rating_less_note_is_a_review_but_not_a_rating(session):
    add_game(session, 1, "Game")
    diary.save_rating(session, 1, None, "just a note")
    stats = compute_stats(session)
    assert (stats.rated, stats.reviews, stats.average_rating) == (0, 1, None)


def test_statuses_and_favorites_are_counted(session):
    for i in range(1, 6):
        add_game(session, i, f"Game {i}")
    diary.set_status(session, 1, "playing")
    diary.set_status(session, 2, "played")
    diary.set_status(session, 3, "played")
    diary.set_status(session, 4, "dropped")
    diary.set_favorite(session, 2, True)
    diary.set_favorite(session, 5, True)  # a favorite needs no status
    stats = compute_stats(session)
    assert stats.status_counts == {"playing": 1, "played": 2, "dropped": 1}
    assert stats.favorites == 2


def test_finished_games_are_counted_by_year_and_undated_ones_separately(session):
    for i in range(1, 7):
        add_game(session, i, f"Game {i}")
    diary.set_status(session, 1, "played", None, date(2024, 12, 31))
    diary.set_status(session, 2, "played", None, date(2025, 1, 1))
    diary.set_status(session, 3, "played", None, date(2025, 8, 9))
    diary.set_status(session, 4, "played", None, None)  # no date known
    diary.set_status(session, 5, "dropped", None, date(2025, 3, 3))  # dropped is not "finished"
    diary.set_status(session, 6, "playing", date(2025, 3, 3))
    stats = compute_stats(session)
    assert stats.finished_by_year == [(2024, 1), (2025, 2)]
    assert stats.finished_undated == 1
