"""Games you played somewhere other than your Steam account: on a console, another PC store, a
friend's copy. You add them by hand, and only games that are also on Steam, so their store
details, tags, reviews and length come from Steam like every other game's.

Such a game is in your library with the platform you played it on, and like a Family Sharing
game it has no Steam playtime or achievements: you type its hours. It counts as played (you
added it because you did), so it is never recommended back to you; your rating or your hours
teach your taste as they do for any game. If you later buy it on Steam, the next sync makes it
an ordinary Steam game, with Steam's hours.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from shelflife.models import OFF_TOPIC, Game, LibraryEntry

PLATFORMS = ("PlayStation", "Xbox", "Nintendo Switch", "Another PC store", "Mobile", "Other")
MAX_HOURS = 100_000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _check(platform: str, hours: float | None) -> None:
    if platform not in PLATFORMS:
        raise ValueError(f"Pick a platform: {', '.join(PLATFORMS)}.")
    if hours is not None and not 0 <= hours <= MAX_HOURS:
        raise ValueError(f"Hours must be between 0 and {MAX_HOURS}, got {hours:g}.")


def where_you_have_it(game: Game) -> str:
    """"on Steam", "through Family Sharing" or "on PlayStation", for a game in your library."""
    entry = game.library_entry
    if entry is not None and entry.platform:
        return f"on {entry.platform}"
    return "through Family Sharing" if game.family_shared else "on Steam"


def add_game(session: Session, app_id: int, name: str, platform: str, hours: float | None = None) -> Game:
    """Put a Steam game you played elsewhere into your library. `name` is used if ShelfLife
    doesn't know the game yet; its store details come with the next fetch."""
    _check(platform, hours)
    game = session.get(Game, app_id)
    if game is not None and game.library_entry is not None:
        raise ValueError(f"{game.name} is already in your library ({where_you_have_it(game)}).")
    if game is None:
        game = Game(steam_app_id=app_id, name=" ".join((name or "").split()) or f"App {app_id}")
        session.add(game)
    elif game.type == OFF_TOPIC:  # a search set it aside; now it is yours, so look it up properly
        game.type, game.details_fetched_at = None, None
    session.add(
        LibraryEntry(
            steam_app_id=app_id,
            playtime_total_min=round((hours or 0) * 60),
            playtime_recent_min=0,
            last_synced=_now(),
            platform=platform,
        )
    )
    session.commit()
    return game


def _entry(session: Session, app_id: int) -> LibraryEntry:
    entry = session.get(LibraryEntry, app_id)
    if entry is None or not entry.platform:
        raise ValueError("Only games you added from another platform can be changed or removed here.")
    return entry


def set_platform(session: Session, app_id: int, platform: str) -> None:
    _check(platform, None)
    _entry(session, app_id).platform = platform
    session.commit()


def remove_game(session: Session, app_id: int) -> None:
    """Take a game you added from another platform out of your library. The game stays known
    (your rating and diary are kept), now as a game you don't have."""
    session.delete(_entry(session, app_id))
    session.commit()
