"""Choices you make in the app and expect to find again (kept in the database)."""

from sqlalchemy.orm import Session

from shelflife.models import Setting

# How the taste profile learns from your games.
TASTE_MODES = {
    "mix": "Mix of both",
    "ratings": "Your ratings only",
    "engagement": "How you play only",
}
TASTE_MODE_HELP = {
    "mix": (
        "Both count, for every game. Your taste is learned twice, once from your ratings and once from "
        "how you play (hours, achievements and when you played, each compared with your other games), and "
        "the two are added up, each counting by how well it predicts your own ratings. A game you rated and "
        "played teaches through both. A favorite you did not rate counts as 5 stars, \"not interested\" as 2."
    ),
    "ratings": (
        "Only your star ratings count. Play, favorites and \"not interested\" teach nothing (\"not "
        "interested\" still hides a game), however many hours you put in."
    ),
    "engagement": (
        "Only how you play counts: hours, achievements and when you played, each compared with your other "
        "games. A game you played far less than usual counts as a mild \"not for me\". Your ratings, favorites "
        "and \"not interested\" are ignored for taste (\"not interested\" still hides a game)."
    ),
}
DEFAULT_TASTE_MODE = "mix"


def get_taste_mode(session: Session) -> str:
    row = session.get(Setting, "taste_mode")
    return row.value if row is not None and row.value in TASTE_MODES else DEFAULT_TASTE_MODE


def set_taste_mode(session: Session, mode: str) -> None:
    if mode not in TASTE_MODES:
        raise ValueError(f"Taste mode must be one of {', '.join(TASTE_MODES)}, got {mode!r}")
    row = session.get(Setting, "taste_mode")
    if row is None:
        session.add(Setting(key="taste_mode", value=mode))
    else:
        row.value = mode
    session.commit()


# Games for adults (tags.is_adult) are left out of recommendations and Explore unless you say so.
DEFAULT_HIDE_ADULT = True


def get_hide_adult(session: Session) -> bool:
    row = session.get(Setting, "hide_adult")
    return DEFAULT_HIDE_ADULT if row is None else row.value == "1"


def set_hide_adult(session: Session, hide: bool) -> None:
    row = session.get(Setting, "hide_adult")
    if row is None:
        session.add(Setting(key="hide_adult", value="1" if hide else "0"))
    else:
        row.value = "1" if hide else "0"
    session.commit()
