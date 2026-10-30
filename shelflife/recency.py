"""How recent your engagement with a game is: a signed term of the engagement score.

Steam reports only lifetime playtime and a last-played date, and one minute
played today resets "last played" for a game you spent 50 hours on years ago. So
the dates of the achievements you unlocked are used instead: they show when you
were really playing. Each unlock counts with a weight that halves every
HALF_LIFE_YEARS, and the game's "activity" is the average over its unlocks:
1.0 if everything is current, near 0 if it is all from long ago.

Activity becomes engagement points: +RECENCY_POINTS when all your play is
current, 0 when your typical unlock is one half-life old, and -RECENCY_POINTS
when it is ancient. So it can lower a game's score, but only when dates show the
play is old; a game with no date evidence gets 0.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

RECENCY_POINTS = 0.5  # engagement points at the two extremes
HALF_LIFE_YEARS = 3.0  # an unlock this old counts half as much

# "Playing it now": clearly more than a token launch in the last two weeks.
PLAYING_NOW_MIN = 120
PLAYING_NOW_SHARE = 0.10  # of lifetime playtime

# Without achievement dates, a last-played date this old is trustworthy: nothing
# can have been played after it. A recent one is not (it may be a single minute).
STALE_LAST_PLAYED_DAYS = 365


@dataclass(frozen=True)
class Recency:
    activity: float | None  # 0..1, how current your play is; None = no date evidence
    basis: str  # "playing now" | "achievement dates" | "last played" | "no date information"
    detail: str  # a short human-readable explanation

    @property
    def points(self) -> float:
        if self.activity is None:
            return 0.0
        return RECENCY_POINTS * (2 * self.activity - 1)


def _decay(age_years: float) -> float:
    return 0.5 ** (max(age_years, 0.0) / HALF_LIFE_YEARS)


def recency(entry, now: datetime | None = None) -> Recency:
    """Recency of a LibraryEntry, from the best date evidence available."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    recent = entry.playtime_recent_min or 0
    total = entry.playtime_total_min or 0
    if recent >= PLAYING_NOW_MIN or (total and recent / total >= PLAYING_NOW_SHARE):
        return Recency(1.0, "playing now", f"{recent / 60:.1f}h in the last two weeks")

    stamps = entry.unlock_times or []
    if stamps:
        weights = [
            _decay((now - datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)).days / 365.25)
            for t in stamps
        ]
        median = datetime.fromtimestamp(sorted(stamps)[len(stamps) // 2], timezone.utc)
        return Recency(
            sum(weights) / len(weights),
            "achievement dates",
            f"{len(stamps)} achievements, typically unlocked around {median:%Y}",
        )

    if entry.last_played:
        days = (now - entry.last_played).days
        if days >= STALE_LAST_PLAYED_DAYS:
            return Recency(
                _decay(days / 365.25),
                "last played",
                f"last played {entry.last_played:%b %Y}, no achievement dates to refine it",
            )

    return Recency(None, "no date information", "can't tell when it was played")
