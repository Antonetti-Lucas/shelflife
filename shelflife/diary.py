"""Your diary: ratings, reviews, "not interested" marks, and where you are with each
game (status, dates, favorite), kept in the database.

Replaces the data/ratings.csv and data/feedback.csv files; those are only read
once, by import_legacy_csv, to bring existing entries over.
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.models import DiaryEntry, Game

STATUSES = ("playing", "played", "dropped")
STATUS_LABELS = {"playing": "Playing", "played": "Played", "dropped": "Dropped"}

# A favorite counts as this star rating when the game has no rating of its own (a
# rating always wins). A status says nothing about taste: "dropped" would mean a
# story game quit halfway as much as an endless multiplayer game you left after 600
# hours, so only what you say outright (rating, favorite, not interested) counts.
FAVORITE_RATING = 5.0

KEEP = object()  # "leave this field as it is" for set_status


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_ratings(session: Session) -> dict[int, float]:
    """steam_app_id -> rating for every rated game."""
    return {
        e.steam_app_id: e.rating
        for e in session.scalars(select(DiaryEntry).where(DiaryEntry.rating.is_not(None)))
    }


def load_not_interested(session: Session) -> dict[int, str]:
    """steam_app_id -> name for every game marked not interested."""
    rows = session.execute(
        select(DiaryEntry.steam_app_id, Game.name)
        .join(Game, Game.steam_app_id == DiaryEntry.steam_app_id)
        .where(DiaryEntry.not_interested.is_(True))
    )
    return {app_id: name for app_id, name in rows}


def favorite_ratings(session: Session) -> dict[int, float]:
    """steam_app_id -> the rating a favorite stands in for, for favorites without a
    rating of their own."""
    return {
        e.steam_app_id: FAVORITE_RATING
        for e in session.scalars(
            select(DiaryEntry).where(DiaryEntry.rating.is_(None), DiaryEntry.favorite.is_(True))
        )
    }


def tracked_ids(session: Session) -> set[int]:
    """Games you gave a status (playing, played or dropped): you have engaged with
    them, so they are not backlog or discovery candidates."""
    return set(session.scalars(select(DiaryEntry.steam_app_id).where(DiaryEntry.status.is_not(None))))


def load_entries(session: Session) -> dict[int, DiaryEntry]:
    """steam_app_id -> diary entry, for every game that has one."""
    return {e.steam_app_id: e for e in session.scalars(select(DiaryEntry))}


def get_entry(session: Session, app_id: int) -> DiaryEntry | None:
    return session.get(DiaryEntry, app_id)


def _entry(session: Session, app_id: int) -> DiaryEntry:
    if session.get(Game, app_id) is None:
        raise ValueError(f"No game with app id {app_id}")
    entry = session.get(DiaryEntry, app_id)
    if entry is None:
        entry = DiaryEntry(steam_app_id=app_id, not_interested=False, updated_at=_now())
        session.add(entry)
    return entry


def _finish(session: Session, entry: DiaryEntry) -> None:
    """Save the entry, or drop it when nothing is left in it."""
    entry.updated_at = _now()
    if (
        entry.rating is None
        and not entry.not_interested
        and not entry.review
        and not entry.status
        and not entry.favorite
    ):
        if entry in session.new:  # created a moment ago and never saved
            session.expunge(entry)
        else:
            session.delete(entry)
    session.commit()


def save_rating(
    session: Session, app_id: int, rating: float | None, review: str | None = None
) -> None:
    """Set (or clear, with None) a game's rating. review=None leaves the review as is;
    pass "" to clear it."""
    if rating is not None and not (0.5 <= rating <= 5 and (rating * 2).is_integer()):
        raise ValueError(f"Rating must be 0.5 to 5 in half steps, got {rating}")
    entry = _entry(session, app_id)
    entry.rating = rating
    if review is not None:
        entry.review = review.strip() or None
    _finish(session, entry)


def set_not_interested(session: Session, app_id: int, value: bool) -> None:
    entry = _entry(session, app_id)
    entry.not_interested = value
    _finish(session, entry)


def set_status(
    session: Session,
    app_id: int,
    status: str | None,
    started_on: date | None | object = KEEP,
    finished_on: date | None | object = KEEP,
) -> None:
    """Set (or clear, with None) where you are with a game.

    The dates are optional and never filled in for you: leave one as KEEP to keep
    what is stored, give a date to set it, or None for "unknown". Clearing the
    status clears both dates.
    """
    if status is not None and status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}, got {status!r}")
    entry = _entry(session, app_id)
    entry.status = status
    if status is None:
        entry.started_on = entry.finished_on = None
    else:
        if started_on is not KEEP:
            entry.started_on = started_on
        if finished_on is not KEEP:
            entry.finished_on = finished_on
        if entry.started_on and entry.finished_on and entry.finished_on < entry.started_on:
            session.rollback()
            raise ValueError("The finish date is before the start date")
    _finish(session, entry)


def set_favorite(session: Session, app_id: int, value: bool) -> None:
    entry = _entry(session, app_id)
    entry.favorite = value
    _finish(session, entry)


def import_legacy_csv(session: Session) -> tuple[int, int]:
    """One-time import of data/ratings.csv and data/feedback.csv.

    Never overwrites something already in the diary. Returns
    (ratings imported, not-interested marks imported).
    """
    from shelflife.feedback import load_not_interested as legacy_marks
    from shelflife.ratings import load_ratings as legacy_ratings

    rated = marked = 0
    for app_id, rating in legacy_ratings().items():
        if session.get(Game, app_id) is None:
            continue
        entry = session.get(DiaryEntry, app_id)
        if entry is None or entry.rating is None:
            save_rating(session, app_id, rating)
            rated += 1
    for app_id in legacy_marks():
        if session.get(Game, app_id) is None:
            continue
        entry = session.get(DiaryEntry, app_id)
        if entry is None or not entry.not_interested:
            set_not_interested(session, app_id, True)
            marked += 1
    return rated, marked
