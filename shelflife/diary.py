"""Your diary: ratings, reviews and "not interested" marks, kept in the database.

Replaces the data/ratings.csv and data/feedback.csv files; those are only read
once, by import_legacy_csv, to bring existing entries over.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.models import DiaryEntry, Game


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
    if entry.rating is None and not entry.not_interested and not entry.review:
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
