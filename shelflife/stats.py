"""Numbers about your library and your diary, for the stats page."""

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from shelflife.diary import STATUSES
from shelflife.models import DiaryEntry, Game, LibraryEntry
from shelflife.recommend import BACKLOG_MAX_MIN, is_played

TOP_HOURS = 10
RATING_STEPS = [r / 2 for r in range(1, 11)]  # 0.5 ... 5.0


@dataclass(frozen=True)
class Stats:
    owned: int
    family: int
    other: int  # games you added from other platforms
    played: int  # of the above, the ones you have really played
    backlog: int  # yours (on Steam or another platform) and under an hour
    hours_total: float  # owned games, plus family hours you entered
    top_hours: list[tuple[Game, float]]

    status_counts: dict[str, int]  # every status, zero included
    favorites: int
    rated: int
    average_rating: float | None
    rating_counts: dict[float, int]  # every half-star step, zero included
    reviews: int
    finished_by_year: list[tuple[int, int]]  # games marked played, by year, oldest first
    finished_undated: int


def compute_stats(session: Session) -> Stats:
    rows = session.execute(
        select(Game, LibraryEntry)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(or_(Game.type == "game", Game.type.is_(None)))
    ).all()

    owned = family = other = played = backlog = 0
    hours: list[tuple[Game, float]] = []
    for game, entry in rows:
        is_family = bool(game.family_shared)
        is_other = bool(entry.platform) and not is_family  # a family game played elsewhere counts as family
        family += is_family
        other += is_other
        owned += not (is_family or is_other)
        played += is_played(entry, is_family)
        backlog += not is_family and entry.playtime_total_min < BACKLOG_MAX_MIN  # other platforms' unplayed ones too
        if entry.playtime_total_min:
            hours.append((game, entry.playtime_total_hours))
    hours.sort(key=lambda gh: gh[1], reverse=True)

    entries = list(session.scalars(select(DiaryEntry)))
    ratings = [e.rating for e in entries if e.rating is not None]
    by_status = Counter(e.status for e in entries if e.status)
    years = Counter(e.finished_on.year for e in entries if e.status == "played" and e.finished_on)

    return Stats(
        owned=owned,
        family=family,
        other=other,
        played=played,
        backlog=backlog,
        hours_total=sum(h for _, h in hours),
        top_hours=hours[:TOP_HOURS],
        status_counts={s: by_status.get(s, 0) for s in STATUSES},
        favorites=sum(1 for e in entries if e.favorite),
        rated=len(ratings),
        average_rating=sum(ratings) / len(ratings) if ratings else None,
        rating_counts={step: sum(1 for r in ratings if r == step) for step in RATING_STEPS},
        reviews=sum(1 for e in entries if e.review),
        finished_by_year=sorted(years.items()),
        finished_undated=sum(1 for e in entries if e.status == "played" and not e.finished_on),
    )
