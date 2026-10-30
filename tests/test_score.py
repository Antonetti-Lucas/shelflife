"""The engagement score: a sum of points from playtime, achievements and recency."""

import pytest

from shelflife.score import engagement_score
from tests.factories import add_game


def entry_of(session, **kwargs):
    return add_game(session, 1, **kwargs).library_entry


def test_playtime_is_logarithmic(session):
    assert engagement_score(entry_of(session, minutes=99 * 60)).playtime == pytest.approx(2.0)


def test_ten_times_the_hours_adds_one_point_not_ten_times_as_many(session):
    ten = add_game(session, 1, minutes=9 * 60).library_entry
    hundred = add_game(session, 2, minutes=99 * 60).library_entry
    assert engagement_score(hundred).playtime - engagement_score(ten).playtime == pytest.approx(1.0)


def test_a_thousand_hours_is_three_points(session):
    assert engagement_score(entry_of(session, minutes=999 * 60)).playtime == pytest.approx(3.0)


def test_no_playtime_gives_no_playtime_points(session):
    assert engagement_score(entry_of(session, minutes=0)).playtime == 0


def test_achievement_share_counts_up_to_two_points(session):
    assert engagement_score(entry_of(session, achievements=(5, 20))).achievements == pytest.approx(0.5)


def test_all_achievements_give_the_full_two_points(session):
    assert engagement_score(entry_of(session, achievements=(20, 20))).achievements == pytest.approx(2.0)


def test_games_with_fewer_than_ten_achievements_are_not_scored_on_them(session):
    # 9/9 is "100%" but says little: a one-achievement game would always max out.
    assert engagement_score(entry_of(session, achievements=(9, 9))).achievements == 0


@pytest.mark.parametrize(
    "rarest, points",
    [(4.0, 1.0), (7.0, 0.5), (5.0, 0.5), (10.0, 0.0), (41.0, 0.0), (None, 0.0)],
)
def test_rare_achievement_bonus(session, rarest, points):
    assert engagement_score(entry_of(session, rarest=rarest)).rare == points


def test_played_in_the_last_two_weeks_adds_half_a_point(session):
    assert engagement_score(entry_of(session, recent=1)).still_playing == 0.5


def test_not_played_recently_adds_nothing_for_still_playing(session):
    assert engagement_score(entry_of(session, recent=0)).still_playing == 0


def test_total_is_the_sum_of_the_parts(session):
    b = engagement_score(entry_of(session, minutes=600, recent=200, achievements=(10, 20), rarest=3.0))
    assert b.total == pytest.approx(b.playtime + b.achievements + b.rare + b.still_playing + b.recency)


def test_a_game_with_no_data_scores_zero(session):
    assert engagement_score(entry_of(session)).total == 0


def test_playing_right_now_raises_recency(session):
    assert engagement_score(entry_of(session, minutes=6000, recent=300)).recency > 0
