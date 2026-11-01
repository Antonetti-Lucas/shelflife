"""Choices you make in the app and expect to find again (kept in the database)."""

from sqlalchemy.orm import Session

from shelflife.models import Setting

# Whether your taste also learns from games you played but didn't rate (shelflife/recommend.py):
# each counts as a likely like, the more surely the more you played it. Off: ratings only.
DEFAULT_LEARN_FROM_PLAY = True
LEARN_FROM_PLAY_HELP = (
    "Each game you played but didn't rate counts as a game you probably liked, and the more you played "
    "it (compared with your other games and with how long its own players play it), the more it counts. "
    "Playing a game a little never counts against it. Off: only your star ratings teach your taste."
)

# Games for adults (tags.is_adult) are left out of recommendations and Explore unless you say so.
DEFAULT_HIDE_ADULT = True


def _flag(session: Session, key: str, default: bool) -> bool:
    row = session.get(Setting, key)
    return default if row is None else row.value == "1"


def _set_flag(session: Session, key: str, value: bool) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value="1" if value else "0"))
    else:
        row.value = "1" if value else "0"
    session.commit()


def get_learn_from_play(session: Session) -> bool:
    return _flag(session, "learn_from_play", DEFAULT_LEARN_FROM_PLAY)


def set_learn_from_play(session: Session, learn: bool) -> None:
    _set_flag(session, "learn_from_play", learn)


def get_hide_adult(session: Session) -> bool:
    return _flag(session, "hide_adult", DEFAULT_HIDE_ADULT)


def set_hide_adult(session: Session, hide: bool) -> None:
    _set_flag(session, "hide_adult", hide)
