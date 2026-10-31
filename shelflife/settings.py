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
        "Both count. A game you rated counts by your rating. A game you played but did not rate "
        "counts by how you played it (hours, achievements, rare ones, recency), as the rating that "
        "play has meant in your own rated games: if the games you play most are the ones you rate "
        "high, play counts for more; if not, for less."
    ),
    "ratings": (
        "Only what you say counts: ratings, favorites and \"not interested\". A game you did not rate "
        "teaches nothing, however many hours you put in."
    ),
    "engagement": (
        "Only how you play counts: hours, achievements, rare achievements and recency. Your ratings and "
        "favorites are ignored for taste (\"not interested\" still hides a game)."
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
