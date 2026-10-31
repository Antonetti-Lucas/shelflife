"""How much your play of a game says you liked it, measured against your own library.

The engagement score (score.py) adds up points, and fixed points mean different things in
different libraries: in one owner's, most played games scored under 2 points and none reached
5, and games without achievements could never score high at all. So here each signal is
ranked among the games you have played instead (a percentile, 0 to 1), and a game's play score
is the weighted average of the signals it has. A missing signal is left out, not counted as
zero: a game without achievements is judged on its hours.

  hours        how long you played it, against your other played games
  completion   the share of its achievements you unlocked (games with 10+ achievements)
  rarity       how rare the rarest achievement you hold is (rarer = more devoted)
  recency      when you played (recency.py): a small shift up for current play, down for
               play that is long past

0.5 is your typical played game. Below it, your play suggests you liked it less than usual;
the taste learns that as a mild "not for me" (play can't say you hated a game: see PLAY_DISLIKE_MAX).
"""

from collections.abc import Iterable
from dataclasses import dataclass

from shelflife.models import LibraryEntry
from shelflife.recency import recency
from shelflife.score import MIN_ACHIEVEMENTS

HOURS_WEIGHT = 1.0
COMPLETION_WEIGHT = 1.0
RARITY_WEIGHT = 0.5
RECENCY_SHIFT = 0.1  # all play current: +0.1; all play long ago: -0.1 (about the 0.5 recency points of the engagement score)
PLAY_DISLIKE_MAX = 0.5  # the least-played game counts as at most this much "not for me" (-1 would be a 1-star rating)
BACKLOG_MAX_MIN = 60  # owned and played under an hour: not played yet, so no verdict


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
    parts: dict[str, float]  # each signal's percentile among your played games
    recency: float  # the shift applied for when you played

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
        average = sum(WEIGHTS[n] * p for n, p in parts.items()) / sum(WEIGHTS[n] for n in parts)
        activity = recency(entry).activity
        shift = 0.0 if activity is None else RECENCY_SHIFT * (2 * activity - 1)
        return PlayScore(value=min(max(average + shift, 0.0), 1.0), parts=parts, recency=shift)
