"""How much your play of a game says you liked it, measured against your own library."""

from datetime import datetime, timedelta

import pytest

from shelflife.models import LibraryEntry
from shelflife.play import PLAY_DISLIKE_MAX, RECENCY_SHIFT, PlayScale, PlayScore, is_played


def entry(hours=0.0, achievements=None, rarest=None, recent_min=0, unlock_years_ago=None):
    e = LibraryEntry(
        steam_app_id=1,
        playtime_total_min=round(hours * 60),
        playtime_recent_min=recent_min,
        rarest_unlocked_pct=rarest,
        last_synced=datetime(2026, 6, 1),
    )
    if achievements:
        e.achievements_unlocked, e.achievements_total = achievements
    if unlock_years_ago is not None:
        when = datetime.now() - timedelta(days=365.25 * unlock_years_ago)
        e.unlock_times = [int(when.timestamp())] * 3
    return e


def test_hours_are_ranked_against_your_other_played_games():
    scale = PlayScale([entry(h) for h in (2, 5, 10, 40, 100)])
    assert scale.score(entry(100)).parts["hours"] == pytest.approx(0.9)  # above four, tied with one
    assert scale.score(entry(2)).parts["hours"] == pytest.approx(0.1)
    assert scale.score(entry(10)).value == pytest.approx(0.5)  # the middle game is your typical one


def test_the_same_hours_mean_more_in_a_library_of_short_sessions():
    light = PlayScale([entry(h) for h in (1, 2, 3, 4)])
    heavy = PlayScale([entry(h) for h in (100, 200, 300, 400)])
    assert light.score(entry(20)).value > heavy.score(entry(20)).value


def test_a_game_without_achievements_is_judged_on_its_hours_alone_not_held_down():
    scale = PlayScale([entry(h, achievements=(5, 50)) for h in (2, 5, 10)] + [entry(200)])
    no_achievements = scale.score(entry(200))
    assert set(no_achievements.parts) == {"hours"} and no_achievements.value > 0.8


def test_completing_a_game_and_holding_rare_achievements_count():
    scale = PlayScale([entry(10, achievements=(n, 20), rarest=r) for n, r in ((2, 60.0), (10, 30.0), (20, 1.0))])
    devoted = scale.score(entry(10, achievements=(20, 20), rarest=1.0))
    casual = scale.score(entry(10, achievements=(2, 20), rarest=60.0))
    assert devoted.value > 0.5 > casual.value


def test_a_game_with_fewer_than_ten_achievements_is_not_judged_by_them():
    scale = PlayScale([entry(10, achievements=(1, 5))])
    assert "completion" not in scale.score(entry(10, achievements=(5, 5))).parts


def test_recent_play_shifts_the_score_up_and_old_play_down_a_little():
    scale = PlayScale([entry(h) for h in (5, 10, 20)])
    now = scale.score(entry(10, recent_min=300))
    old = scale.score(entry(10, unlock_years_ago=15))
    assert now.recency == pytest.approx(RECENCY_SHIFT)
    assert -RECENCY_SHIFT <= old.recency < 0
    assert now.value > scale.score(entry(10)).value > old.value


def test_nothing_known_about_your_play_means_no_score():
    assert PlayScale([entry(5)]).score(entry(0)) is None


def test_play_can_say_not_for_me_but_never_hated():
    assert PlayScore(1.0, {}, 0.0).weight == pytest.approx(1.0)
    assert PlayScore(0.5, {}, 0.0).weight == 0
    assert PlayScore(0.0, {}, 0.0).weight == pytest.approx(-PLAY_DISLIKE_MAX)


def test_an_empty_library_ranks_everything_as_typical():
    assert PlayScale([]).score(entry(10)).value == pytest.approx(0.5)


@pytest.mark.parametrize(
    "minutes, family, unlocked, played",
    [(60, False, 0, True), (59, False, 0, False), (0, True, 1, True), (0, True, 0, False), (59, True, 0, False)],
)
def test_what_counts_as_played(minutes, family, unlocked, played):
    e = entry(minutes / 60, achievements=(unlocked, 10) if unlocked else None)
    assert is_played(e, family) is played
