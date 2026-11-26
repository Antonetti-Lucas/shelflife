"""Games you played somewhere other than your Steam account: on a console, another PC store, a
friend's copy. You add them by hand, and only games that are also on Steam, so their store
details, tags, reviews and length come from Steam like every other game's.

Such a game is in your library with the platform you have it on, and like a Family Sharing
game it has no Steam playtime or achievements: you type its hours. With an hour or more it is
played, and your rating or your hours teach your taste as for any game; under an hour it is on
your backlog, recommended like an owned game you haven't played, until you play it, rate it or
give it a status. If you later buy it on Steam, the next sync makes it
an ordinary Steam game, with Steam's hours.

A Family Sharing game you played somewhere else instead is marked the same way: it stays a
family game, and the platform says where you really played it (your hours are for anywhere).
Its Steam achievements are no longer read or counted, since they'd show your Steam play (often
none), not your play there. If the family stops sharing it, it stays in your library as a game
from that platform.

A game that is not on Steam at all comes from IGDB (shelflife/igdb.py) and is logged the same way:
its platform is any IGDB lists for it (Atari 2600, PlayStation, Wii...), your hours decide
played or backlog, and Steam never reads it.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from shelflife.models import OFF_TOPIC, Game, LibraryEntry, on_steam

PLATFORMS = ("PlayStation", "Xbox", "Nintendo Switch", "Another PC store", "Mobile", "Other")
MAX_HOURS = 100_000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def platforms_for(game: Game | None) -> tuple[str, ...]:
    """The platforms you can say you have this game on: IGDB's for a game that is not on Steam."""
    if game is not None and not game.on_steam:
        return tuple((game.igdb or {}).get("platforms") or ()) + ("Other",)
    return PLATFORMS


def _check(platform: str, hours: float | None, allowed: tuple[str, ...] = PLATFORMS) -> None:
    if platform not in allowed:
        raise ValueError(f"Pick a platform: {', '.join(allowed)}.")
    if hours is not None and not 0 <= hours <= MAX_HOURS:
        raise ValueError(f"Hours must be between 0 and {MAX_HOURS}, got {hours:g}.")


def where_you_have_it(game: Game) -> str:
    """"on Steam", "through Family Sharing", "on PlayStation" or "through Family Sharing, played on
    PlayStation", for a game in your library."""
    entry = game.library_entry
    platform = entry.platform if entry is not None else None
    if game.family_shared:
        return f"through Family Sharing, played on {platform}" if platform else "through Family Sharing"
    return f"on {platform}" if platform else "on Steam"


def elsewhere(game: Game) -> str | None:
    """The other platform you have this game on, or None. A game on your Steam wishlist that you
    already have elsewhere gets a warning, so you don't buy it twice by mistake."""
    entry = game.library_entry
    return entry.platform if entry is not None and entry.platform else None


def can_mark(game: Game | None) -> bool:
    """A Family Sharing game not yet marked as played elsewhere: it can be."""
    entry = game.library_entry if game is not None else None
    return entry is not None and bool(game.family_shared) and not entry.platform


def add_game(session: Session, app_id: int, name: str, platform: str, hours: float | None = None) -> Game:
    """Put a Steam game you played elsewhere into your library. `name` is used if ShelfLife
    doesn't know the game yet; its store details come with the next fetch."""
    if not on_steam(app_id):
        raise ValueError("That is not a Steam game: add it from IGDB.")
    _check(platform, hours)
    game = session.get(Game, app_id)
    if can_mark(game):  # in your family library, but you played it elsewhere
        game.library_entry.platform = platform
        if hours is not None:
            game.library_entry.playtime_total_min = round(hours * 60)
        session.commit()
        return game
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


def add_igdb_game(session: Session, found, platform: str, hours: float | None = None) -> Game:
    """Put a game that is not on Steam into your library, from what IGDB says about it
    (`found`: an igdb.IgdbGame). A game IGDB links to Steam is added as that Steam game."""
    from shelflife import igdb

    if found.steam_id:
        return add_game(session, found.steam_id, found.name, platform if platform in PLATFORMS else "Other", hours)
    _check(platform, hours, tuple(found.platforms) + ("Other",))
    game = session.get(Game, igdb.game_id(found.id))
    if game is not None and game.library_entry is not None:
        raise ValueError(f"{game.name} is already in your library ({where_you_have_it(game)}).")
    game = igdb.upsert(session, found)
    session.flush()
    session.add(
        LibraryEntry(
            steam_app_id=game.steam_app_id,
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
    game = session.get(Game, app_id)
    if entry is None or not (entry.platform or game.family_shared):
        raise ValueError("Only family games and games added from another platform can be changed here.")
    return entry


def set_platform(session: Session, app_id: int, platform: str) -> None:
    """Change where you played it. For a family game, no platform means you played it through
    Family Sharing after all."""
    entry = _entry(session, app_id)
    if not platform and session.get(Game, app_id).family_shared:
        entry.platform = None
    else:
        _check(platform, None, platforms_for(session.get(Game, app_id)))
        entry.platform = platform
    session.commit()


def remove_game(session: Session, app_id: int) -> None:
    """Take a game you added from another platform out of your library. The game stays known
    (your rating and diary are kept), now as a game you don't have. A family game stays in your
    library, only no longer marked as played elsewhere."""
    entry = _entry(session, app_id)
    if session.get(Game, app_id).family_shared:
        entry.platform = None
    else:
        session.delete(entry)
    session.commit()
