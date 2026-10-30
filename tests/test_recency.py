"""Recency: when you really played, judged from achievement dates, not "last played"."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shelflife.recency import RECENCY_POINTS, Recency, recency

NOW = datetime(2026, 6, 1)


def stamp(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def years_ago(n):
    return NOW - timedelta(days=round(n * 365.25))


def entry(**overrides):
    values = dict(playtime_recent_min=0, playtime_total_min=6000, unlock_times=None, last_played=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_points_range_from_plus_to_minus_the_maximum():
    assert Recency(1.0, "x", "").points == RECENCY_POINTS
    assert Recency(0.0, "x", "").points == -RECENCY_POINTS
    assert Recency(0.5, "x", "").points == 0


def test_no_evidence_means_zero_points_never_a_penalty():
    assert Recency(None, "none", "").points == 0


def test_clearly_playing_now_counts_as_fully_current():
    r = recency(entry(playtime_recent_min=180), NOW)
    assert (r.basis, r.activity, r.points) == ("playing now", 1.0, RECENCY_POINTS)


def test_ten_percent_of_lifetime_playtime_in_two_weeks_is_playing_now():
    assert recency(entry(playtime_total_min=600, playtime_recent_min=60), NOW).basis == "playing now"


def test_a_token_launch_is_not_playing_now():
    # One minute today resets Steam's "last played" but says nothing about when the
    # other 100 hours happened, so there is no evidence either way.
    r = recency(entry(playtime_recent_min=1, last_played=NOW - timedelta(days=1)), NOW)
    assert r.activity is None
    assert r.points == 0


def test_recent_unlocks_give_positive_points():
    r = recency(entry(unlock_times=[stamp(years_ago(0.1))] * 5), NOW)
    assert r.basis == "achievement dates"
    assert r.activity > 0.95
    assert r.points > 0.4


def test_an_unlock_one_half_life_old_is_neutral():
    r = recency(entry(unlock_times=[stamp(years_ago(3))]), NOW)
    assert r.activity == pytest.approx(0.5, abs=0.01)
    assert r.points == pytest.approx(0, abs=0.01)


def test_unlocks_from_a_decade_ago_give_negative_points():
    r = recency(entry(unlock_times=[stamp(years_ago(12))] * 4), NOW)
    assert r.activity == pytest.approx(0.0625, abs=0.01)
    assert r.points < 0


def test_a_player_who_returns_scores_in_between():
    stamps = [stamp(years_ago(10))] * 10 + [stamp(years_ago(0.1))] * 10
    assert 0.4 < recency(entry(unlock_times=stamps), NOW).activity < 0.6


def test_unlock_dates_beat_a_misleading_last_played_date():
    # The Garry's Mod case: last played yesterday (a minute), unlocks all years ago.
    r = recency(
        entry(playtime_recent_min=1, last_played=NOW - timedelta(days=1), unlock_times=[stamp(years_ago(10))] * 9),
        NOW,
    )
    assert r.basis == "achievement dates"
    assert r.points < 0


def test_an_old_last_played_date_is_trusted_when_there_are_no_unlocks():
    r = recency(entry(last_played=years_ago(6)), NOW)
    assert r.basis == "last played"
    assert r.points == pytest.approx(-RECENCY_POINTS / 2, abs=0.02)  # activity 0.25


def test_a_recent_last_played_date_alone_is_not_trusted():
    r = recency(entry(last_played=years_ago(0.5)), NOW)
    assert r.basis == "no date information"
    assert r.points == 0


def test_the_explanation_mentions_the_typical_year():
    r = recency(entry(unlock_times=[stamp(datetime(2016, 5, 1))] * 3), NOW)
    assert "2016" in r.detail
