"""Choices you make in the app and expect to find again (kept in the database)."""

from sqlalchemy.orm import Session

from shelflife.models import Setting

# What teaches your taste (shelflife/recommend.py). "both": your star ratings, and games you played
# but didn't rate as likely likes, the more surely the more you played them. "ratings": star ratings
# only. "play": how you played only, star ratings ignored. "not interested" counts in every mode.
TASTE_MODES = ("both", "ratings", "play")
DEFAULT_TASTE_MODE = "both"
TASTE_MODE_LABELS = {
    "both": ("Ratings and play", "Your star ratings, plus the games you played but didn't rate: the more you played one, the more it counts."),
    "ratings": ("Ratings only", "Only your star ratings. Games you played without rating are ignored."),
    "play": ("Play only", "Only how much you played each game, against your other games and its own players. Star ratings are ignored."),
}

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


def get_taste_mode(session: Session) -> str:
    row = session.get(Setting, "taste_mode")
    if row is not None and row.value in TASTE_MODES:
        return row.value
    legacy = session.get(Setting, "learn_from_play")  # the on/off switch that came before the modes
    return "ratings" if legacy is not None and legacy.value == "0" else DEFAULT_TASTE_MODE


def set_taste_mode(session: Session, mode: str) -> None:
    if mode not in TASTE_MODES:
        raise ValueError(f"Taste mode must be one of {', '.join(TASTE_MODES)}, got {mode!r}")
    row = session.get(Setting, "taste_mode")
    if row is None:
        session.add(Setting(key="taste_mode", value=mode))
    else:
        row.value = mode
    session.commit()


def get_hide_adult(session: Session) -> bool:
    return _flag(session, "hide_adult", DEFAULT_HIDE_ADULT)


def set_hide_adult(session: Session, hide: bool) -> None:
    _set_flag(session, "hide_adult", hide)
