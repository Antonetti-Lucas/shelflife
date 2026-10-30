"""Engagement score: how much you seem to like a game, as a sum of points.

Used as the implicit rating for games without an explicit one. Every signal
adds points independently; the weights below are starting guesses to be tuned
against your own ratings and how the recommendations feel.
"""

import math
from dataclasses import dataclass

from shelflife.models import LibraryEntry
from shelflife.recency import recency as recency_of

PLAYTIME_WEIGHT = 1.0  # x log10(1 + hours): 10h = 1.0, 100h = 2.0, 1000h = 3.0

ACHIEVEMENT_MAX = 2.0  # x share of achievements unlocked
MIN_ACHIEVEMENTS = 10  # fewer than this and the share says little (1/1 = 100%)

# (rarest achievement you hold is earned by fewer than X% of players, points)
RARE_BONUS = ((5.0, 1.0), (10.0, 0.5))

STILL_PLAYING = 0.5  # played in the last two weeks

# Recency (when you really played, from achievement dates) adds or subtracts up to
# RECENCY_POINTS; see recency.py.


@dataclass(frozen=True)
class ScoreBreakdown:
    playtime: float = 0.0
    achievements: float = 0.0
    rare: float = 0.0
    still_playing: float = 0.0
    recency: float = 0.0  # signed: positive for current play, negative for old

    @property
    def total(self) -> float:
        return (
            self.playtime
            + self.achievements
            + self.rare
            + self.still_playing
            + self.recency
        )


def engagement_score(entry: LibraryEntry) -> ScoreBreakdown:
    """Points for one library entry. Missing data adds nothing; recency is the one
    signed term, and only dates showing old play can lower the score."""
    playtime = PLAYTIME_WEIGHT * math.log10(1 + entry.playtime_total_hours)

    achievements = rare = 0.0
    total = entry.achievements_total or 0
    if total >= MIN_ACHIEVEMENTS:
        achievements = ACHIEVEMENT_MAX * (entry.achievements_unlocked or 0) / total

    if entry.rarest_unlocked_pct is not None:
        for threshold, points in RARE_BONUS:
            if entry.rarest_unlocked_pct < threshold:
                rare = points
                break

    return ScoreBreakdown(
        playtime=playtime,
        achievements=achievements,
        rare=rare,
        still_playing=STILL_PLAYING if entry.playtime_recent_min > 0 else 0.0,
        recency=recency_of(entry).points,
    )
