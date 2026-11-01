"""How much your play of a game says you liked it, measured against your own library.

The engagement score (score.py) adds up points, and fixed points mean different things in
different libraries: in one owner's, most played games scored under 2 points and none reached
5, and games without achievements could never score high at all. So here each signal is
ranked among the games you have played instead (a percentile, 0 to 1), and a game's play score
is the weighted average of the signals it has. A missing signal is left out, not counted as
zero: a game without achievements is judged on its hours.

  hours        how long you played it, against the game's own players: the hours its reviewers
               played (reviews.py), so two hours of a short game and a hundred of an endless one
               are each judged by what is usual for that game. Percentiles, never averages, so
               a few reviewers with thousands of idle hours move only the top end. Blended with
               your hours against your other played games, more so when few reviewers are known.
  completion   the share of its achievements you unlocked (games with 10+ achievements)
  rarity       how rare the rarest achievement you hold is (rarer = more devoted)
  recency      when you played (recency.py): a small shift up for current play, down for
               play that is long past

0.5 is your typical played game. Below it, your play suggests you liked it less than usual;
the taste learns that as a mild "not for me" (play can't say you hated a game: see PLAY_DISLIKE_MAX).
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

from shelflife.models import LibraryEntry
from shelflife.recency import recency
from shelflife.reviews import PLAYER_PERCENTILES
from shelflife.score import MIN_ACHIEVEMENTS

HOURS_WEIGHT = 1.0
COMPLETION_WEIGHT = 1.0
RARITY_WEIGHT = 0.5
RECENCY_SHIFT = 0.1  # all play current: +0.1; all play long ago: -0.1 (about the 0.5 recency points of the engagement score)
PLAY_DISLIKE_MAX = 0.5  # the least-played game counts as at most this much "not for me" (-1 would be a 1-star rating)
BACKLOG_MAX_MIN = 60  # owned and played under an hour: not played yet, so no verdict
# How far the game's own players are trusted over your library: sample / (sample + this), so a full
# sample of 500 reviewers counts half and a thin one less. Not more than half: reviewers' hours are not
# a game's length. People who love a game keep playing it (Half-Life 2's reviewers: median 27 hours,
# well past its ending), so judged by them alone, a story game you finished once and loved looks
# barely played. Checked on the owner's games: at up to half, play agrees with ratings as before
# (+0.340 against +0.341) and both play-only and the mix predict better; at up to five sixths, play
# agreed less (+0.309). It still pulls down endless and idle games you over-played (TBH: Task Bar Hero).
PLAYER_SAMPLE_TRUST = 500


def players_percentile(hours: float, percentiles: list[float]) -> float:
    """Where `hours` falls among a game's players, 0 to 1, from their hours at PLAYER_PERCENTILES.
    Between the known points it goes in a straight line on a log scale (10 to 20 hours is as big
    a step as 100 to 200); below the lowest it falls to 0 at no hours, and above the highest it
    rises toward 1 as slowly as the top quarter of players spreads out."""
    points = [(0.0, 0.0)]
    for p, h in zip(PLAYER_PERCENTILES, percentiles):
        x = math.log1p(max(h, 0.0))
        points.append((p, max(x, points[-1][1])))  # never decreasing
    x = math.log1p(max(hours, 0.0))
    for (p0, x0), (p1, x1) in zip(points, points[1:]):
        if x <= x1:
            return p0 if x1 == x0 else p0 + (p1 - p0) * (x - x0) / (x1 - x0)
    top_p, top_x = points[-1]
    spread = max(top_x - points[-3][1], 0.1)  # from the 50th to the 90th percentile, in log hours
    return top_p + (1 - top_p) * min((x - top_x) / spread, 1.0)


def is_played(entry: LibraryEntry | None, family: bool) -> bool:
    """Whether you've really played a game. Steam reports no playtime for family
    games, so for those any unlocked achievement counts as having played."""
    if entry is None:
        return False
    if entry.playtime_total_min >= BACKLOG_MAX_MIN:
        return True
    return family and (entry.achievements_unlocked or 0) > 0


def _signals(entry: LibraryEntry) -> dict[str, float]:
    """The raw signals a game has (bigger = more engaged); a missing one is left out."""
    out = {}
    if entry.playtime_total_min > 0:
        out["hours"] = float(entry.playtime_total_min)
    if (entry.achievements_total or 0) >= MIN_ACHIEVEMENTS:
        out["completion"] = (entry.achievements_unlocked or 0) / entry.achievements_total
    if entry.rarest_unlocked_pct is not None:
        out["rarity"] = -entry.rarest_unlocked_pct
    return out


WEIGHTS = {"hours": HOURS_WEIGHT, "completion": COMPLETION_WEIGHT, "rarity": RARITY_WEIGHT}


@dataclass(frozen=True)
class PlayScore:
    value: float  # 0 to 1; 0.5 = your typical played game
    parts: dict[str, float]  # each signal's percentile (hours: against the game's players and your library)
    recency: float  # the shift applied for when you played
    players: float | None = None  # your hours against the game's own players alone, when known

    @property
    def weight(self) -> float:
        """What it teaches the taste, -PLAY_DISLIKE_MAX to +1: 0 for your typical game."""
        centred = 2 * self.value - 1
        return centred if centred >= 0 else centred * PLAY_DISLIKE_MAX


class PlayScale:
    """Percentiles of each signal among the games you have played."""

    def __init__(self, played: Iterable[LibraryEntry]):
        self._sorted: dict[str, list[float]] = {name: [] for name in WEIGHTS}
        for entry in played:
            for name, value in _signals(entry).items():
                self._sorted[name].append(value)
        for values in self._sorted.values():
            values.sort()

    def _percentile(self, name: str, value: float) -> float:
        """Share of your played games below this value, ties counting half (0 to 1)."""
        values = self._sorted[name]
        if not values:
            return 0.5
        below = sum(1 for v in values if v < value)
        equal = sum(1 for v in values if v == value)
        return (below + equal / 2) / len(values)

    def score(self, entry: LibraryEntry) -> PlayScore | None:
        """The play score of a game you played; None when nothing is known about your play."""
        parts = {name: self._percentile(name, value) for name, value in _signals(entry).items()}
        if not parts:
            return None
        players = None
        game = getattr(entry, "game", None)
        if "hours" in parts and game is not None and game.player_hours and game.player_hours_sample:
            players = players_percentile(entry.playtime_total_hours, game.player_hours)
            trust = game.player_hours_sample / (game.player_hours_sample + PLAYER_SAMPLE_TRUST)
            parts["hours"] = trust * players + (1 - trust) * parts["hours"]
        average = sum(WEIGHTS[n] * p for n, p in parts.items()) / sum(WEIGHTS[n] for n in parts)
        activity = recency(entry).activity
        shift = 0.0 if activity is None else RECENCY_SHIFT * (2 * activity - 1)
        return PlayScore(value=min(max(average + shift, 0.0), 1.0), parts=parts, recency=shift, players=players)
