"""Save the Steam library to the database and enrich it with store data."""

from datetime import datetime, timedelta, timezone
from typing import Callable

import requests
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session, selectinload

from shelflife import store
from shelflife.achievements import AchievementsUnavailable, AchievementStats, fetch_achievements
from shelflife.models import OFF_TOPIC, Game, LibraryEntry, PlaytimeSnapshot, WishlistItem
from shelflife.play import is_played
from shelflife import reviews
from shelflife.reviews import fetch_review_counts
from shelflife.steam import OwnedGame, SteamError
from shelflife.throttle import RateLimited


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def upsert_library(session: Session, owned: list[OwnedGame]) -> None:
    """Create/update Game and LibraryEntry rows; keeps already-fetched details.

    Also records a playtime snapshot whenever a game's total changed (and a first
    one for games with none), building the history Steam doesn't provide.
    """
    now = _now()
    snapshotted = set(session.scalars(select(PlaytimeSnapshot.steam_app_id).distinct()))
    for og in owned:
        game = session.get(Game, og.app_id)
        if game is None:
            game = Game(steam_app_id=og.app_id, name=og.name)
            session.add(game)
        else:
            game.name = og.name
            if game.type == OFF_TOPIC:  # a search set it aside; now it is yours, so look it up properly
                game.type, game.details_fetched_at = None, None
        game.family_shared = False  # you own it now, if it was ever a family game

        entry = session.get(LibraryEntry, og.app_id)
        if entry is None:
            entry = LibraryEntry(steam_app_id=og.app_id)
            session.add(entry)
        entry.platform = None  # bought on Steam: a game you added from another platform is now a Steam game
        changed = entry.playtime_total_min != og.playtime_total_min
        if changed or og.app_id not in snapshotted:
            session.add(
                PlaytimeSnapshot(
                    steam_app_id=og.app_id, taken_at=now, playtime_total_min=og.playtime_total_min
                )
            )
        entry.playtime_total_min = og.playtime_total_min
        entry.playtime_recent_min = og.playtime_recent_min
        entry.last_played = (
            datetime.fromtimestamp(og.last_played_ts, timezone.utc).replace(tzinfo=None)
            if og.last_played_ts
            else None
        )
        entry.last_synced = now
    session.commit()


MAX_CONSECUTIVE_FAILURES = 3

# How long before a sync looks at something again. The web page's "check everything
# again" ignores these and re-reads it all.
ACHIEVEMENTS_REFRESH_DAYS = 30  # catches achievements a developer added, and family games
DETAILS_RETRY_DAYS = 7  # a game the store had no page for, or a game with no tags: try again (DLC and software never have tags)
DETAILS_REFRESH_DAYS = 90  # descriptions drift; refreshed a few at a time
DETAILS_REFRESH_BATCH = 30  # at most this many stale games per sync (about a minute and a half)
REVIEWS_REFRESH_DAYS = 30  # review counts grow
RECENT_REREAD_HOURS = 6  # a game played lately is read again, but not minutes after it was read
PLAYER_HOURS_REFRESH_DAYS = 180  # how long a game's players play changes slowly
LENGTH_PAGES = 1  # a game you haven't played: 100 reviews are enough to estimate its length
# Prices matter for games you don't own: your wishlist's on every sync (a sale shows within a day),
# the rest of the pool and Family Sharing games' every week. Games you own aren't refreshed.
WISHLIST_PRICE_HOURS = 20
OTHER_PRICE_DAYS = 7
# Tags drift as players vote. Steam gives 200 games' tags per call, so every game is read again monthly.
TAGS_REFRESH_DAYS = 30
STEAM_TAGS_SINCE = datetime(2026, 11, 2, 12)  # tags read before this came from SteamSpy: read once more from Steam


def achievements_due(
    session: Session, everything: bool = False, refresh_days: int = ACHIEVEMENTS_REFRESH_DAYS
) -> list[LibraryEntry]:
    """The library entries whose achievements a sync should read, most-played first.

    Games you have played (or family games, which show no playtime) that
    - were never read, or
    - you played since they were last read, or in the last two weeks and they were read
      more than RECENT_REREAD_HOURS ago (a session may still be adding achievements), or
    - were read before unlock times were stored, or
    - were last read more than `refresh_days` ago (a developer may have added achievements).
    `everything` reads them all.
    """
    now = _now()
    cutoff = now - timedelta(days=refresh_days)
    recent_cutoff = now - timedelta(hours=RECENT_REREAD_HOURS)
    played_or_family = or_(LibraryEntry.playtime_total_min > 0, Game.family_shared.is_(True))
    # Steam's achievements say nothing about a game played on another platform (family games included).
    conditions = [played_or_family, LibraryEntry.platform.is_(None)]
    if not everything:
        conditions.append(
            or_(
                LibraryEntry.achievements_fetched_at.is_(None),
                and_(LibraryEntry.playtime_recent_min > 0, LibraryEntry.achievements_fetched_at < recent_cutoff),
                and_(LibraryEntry.achievements_unlocked > 0, LibraryEntry.unlock_times.is_(None)),
                LibraryEntry.last_played > LibraryEntry.achievements_fetched_at,
                LibraryEntry.achievements_fetched_at < cutoff,
            )
        )
    return list(
        session.scalars(
            select(LibraryEntry)
            .join(Game, Game.steam_app_id == LibraryEntry.steam_app_id)
            .where(*conditions)
            .order_by(LibraryEntry.playtime_total_min.desc())
        )
    )


def sync_achievements(
    session: Session,
    api_key: str,
    steam_id: str,
    on_progress: Callable[[int, int, Game, AchievementStats], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    everything: bool = False,
) -> int:
    """Fetch achievement progress for the games `achievements_due` lists, most-played
    first. `should_stop` is asked before each game; when it says yes the run ends there,
    keeping what is done. Returns the number fetched.
    """
    pending = achievements_due(session, everything=everything)

    done = failures = 0
    for i, entry in enumerate(pending, start=1):
        if should_stop is not None and should_stop():
            break
        try:
            stats = fetch_achievements(api_key, steam_id, entry.steam_app_id)
            failures = 0
        except AchievementsUnavailable:
            continue  # one game Steam can't answer for; retried on the next run
        except SteamError as exc:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise SteamError(f"{exc} ({failures} failures in a row, stopping.)") from exc
            continue

        entry.achievements_total = stats.total
        entry.achievements_unlocked = stats.unlocked
        entry.rarest_unlocked_pct = stats.rarest_unlocked_pct
        entry.first_unlock_at = stats.first_unlock_at
        entry.last_unlock_at = stats.last_unlock_at
        # [] (unlocks without dates) is kept apart from None so it isn't refetched forever.
        entry.unlock_times = list(stats.unlock_times) if stats.unlocked else None
        entry.achievements_fetched_at = _now()
        session.commit()
        done += 1
        if on_progress:
            on_progress(i, len(pending), entry.game, stats)
    return done


def _fallback_tags(game: Game) -> dict[str, int]:
    """Tags for a game Steam's API has none for: SteamSpy's (named as Steam names them now), or
    the store page's tag votes as a last resort. Best effort: {} when neither answers. SteamSpy
    also supplies review counts when we don't have any yet."""
    try:
        spy = store.fetch_spy_details(game.steam_app_id)
    except RateLimited:
        raise
    except (requests.RequestException, ValueError):
        spy = None
    tags: dict[str, int] = {}
    if spy is not None:
        tags = store.steam_names(spy.tags)
        if game.reviews_fetched_at is None and spy.positive is not None:
            game.reviews_positive = spy.positive
            game.reviews_negative = spy.negative or 0
            game.reviews_fetched_at = _now()
    if not tags:
        try:
            tags = store.fetch_store_page_tags(game.steam_app_id)
        except RateLimited:
            raise
        except (requests.RequestException, ValueError):
            pass
    return tags


def _fill_tags(game: Game) -> None:
    """Steam's tags first, then the fallbacks. Tags are best effort: a failure here shouldn't
    lose the store data."""
    try:
        tags = store.fetch_steam_tags([game.steam_app_id]).get(game.steam_app_id, {})
    except RateLimited:
        raise
    except (requests.RequestException, ValueError):
        tags = {}
    if tags := tags or _fallback_tags(game):
        game.tags = tags
    game.tags_fetched_at = _now()


def enrich_one(game: Game) -> None:
    """Fetch store details for one game (no commit), and its tags when it has none yet (a sync's
    tag step keeps them current, and a search brings them with the game)."""
    details = store.fetch_store_details(game.steam_app_id)
    if details is None:
        if not game.type:  # a game that was fine before keeps what it had: the store may just have hiccuped
            game.type = "unavailable"
        if game.about_text is None:
            game.about_text = ""  # no store page: nothing to read, so do not ask again
    else:
        if details.name and (not game.name or game.name.startswith("App ")):
            game.name = details.name  # family imports start with a placeholder name
        game.type = details.type
        game.short_description = details.short_description
        game.genres = details.genres
        game.release_date = details.release_date
        game.cover_url = details.cover_url
        game.about_text = details.about_text
        game.languages = details.languages
        game.price, game.price_fetched_at = details.price, _now()
        game.store_info = details.info
    game.details_fetched_at = _now()
    if not game.tags:
        _fill_tags(game)


def _fetch_reviews(
    session: Session,
    games: list[Game],
    on_progress: Callable[[int, int, Game], None] | None,
    should_stop: Callable[[], bool] | None,
) -> int:
    done = failures = 0
    for i, game in enumerate(games, start=1):
        if should_stop is not None and should_stop():
            break
        try:
            counts = fetch_review_counts(game.steam_app_id)
            failures = 0
        except RateLimited:
            raise
        except requests.RequestException as exc:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise SteamError(f"{exc} ({failures} failures in a row, stopping.)") from exc
            continue

        game.reviews_positive = counts.positive if counts else None
        game.reviews_negative = counts.negative if counts else None
        game.reviews_fetched_at = _now()
        session.commit()
        done += 1
        if on_progress:
            on_progress(i, len(games), game)
    return done


def sync_backlog_reviews(
    session: Session,
    max_playtime_min: int,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Fetch review counts for owned games played under max_playtime_min (only those
    still without counts). The command-line discovery uses this; the web sync reads
    every game with `sync_all_reviews`. Returns the number fetched.
    """
    pending = list(
        session.scalars(
            select(Game)
            .join(LibraryEntry)
            .where(
                Game.type == "game",
                Game.reviews_fetched_at.is_(None),
                LibraryEntry.playtime_total_min < max_playtime_min,
            )
        )
    )
    return _fetch_reviews(session, pending, on_progress, should_stop)


def reviews_due(
    session: Session, everything: bool = False, refresh_days: int = REVIEWS_REFRESH_DAYS
) -> list[Game]:
    """The library and wishlist games (owned, family or wanted; played or not) whose review counts
    a sync should read: those with none, then those read more than `refresh_days` ago, oldest
    first. `everything` reads them all."""
    cutoff = _now() - timedelta(days=refresh_days)
    query = _wanted_games().where(Game.type == "game").order_by(Game.reviews_fetched_at.asc().nulls_first())
    if not everything:
        query = query.where(or_(Game.reviews_fetched_at.is_(None), Game.reviews_fetched_at < cutoff))
    return list(session.scalars(query))


def sync_all_reviews(
    session: Session,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    everything: bool = False,
) -> int:
    """Fetch review counts for the games `reviews_due` lists. Returns the number fetched."""
    return _fetch_reviews(session, reviews_due(session, everything=everything), on_progress, should_stop)


def player_hours_due(session: Session, everything: bool = False) -> list[Game]:
    """Games you have played (an hour or more, or a family game with an achievement) whose players'
    hours were never read or were read more than PLAYER_HOURS_REFRESH_DAYS ago, most played first.
    They are what your own hours are compared with. `everything` reads them all."""
    played = or_(
        LibraryEntry.playtime_total_min >= 60,
        and_(Game.family_shared.is_(True), LibraryEntry.achievements_unlocked > 0),
    )
    query = (
        select(Game)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(Game.type == "game", played)
        .order_by(LibraryEntry.playtime_total_min.desc())
    )
    if not everything:
        cutoff = _now() - timedelta(days=PLAYER_HOURS_REFRESH_DAYS)
        query = query.where(or_(Game.player_hours_fetched_at.is_(None), Game.player_hours_fetched_at < cutoff))
    return list(session.scalars(query))


def length_due(session: Session, everything: bool = False) -> list[Game]:
    """Games you haven't played whose players' hours were never read or are PLAYER_HOURS_REFRESH_DAYS
    old: only to estimate how long they are (shelflife/length.py), so a light sample. Your wishlist
    first, then your library, then the rest of the pool, most reviewed first. A game you start
    playing keeps its light sample until its next refresh."""
    played = or_(
        LibraryEntry.playtime_total_min >= 60,
        and_(Game.family_shared.is_(True), func.coalesce(LibraryEntry.achievements_unlocked, 0) > 0),
    )
    query = (
        select(Game)
        .outerjoin(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .outerjoin(WishlistItem, WishlistItem.steam_app_id == Game.steam_app_id)
        .where(Game.type == "game", or_(LibraryEntry.steam_app_id.is_(None), not_(played)))
        .order_by(
            WishlistItem.steam_app_id.is_(None),
            LibraryEntry.steam_app_id.is_(None),
            (func.coalesce(Game.reviews_positive, 0) + func.coalesce(Game.reviews_negative, 0)).desc(),
        )
    )
    if not everything:
        cutoff = _now() - timedelta(days=PLAYER_HOURS_REFRESH_DAYS)
        query = query.where(or_(Game.player_hours_fetched_at.is_(None), Game.player_hours_fetched_at < cutoff))
    return list(session.scalars(query))


def sync_player_hours(
    session: Session,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    everything: bool = False,
) -> int:
    """Read how long each due game's reviewers played it: up to five pages of reviews for a game
    you played (your hours are measured against them), one page for one you haven't (for its
    length). Saves game by game. Returns the number read."""
    pending = [(g, reviews.PLAYER_PAGES) for g in player_hours_due(session, everything=everything)]
    pending += [(g, LENGTH_PAGES) for g in length_due(session, everything=everything)]
    done = failures = 0
    for i, (game, pages) in enumerate(pending, start=1):
        if should_stop is not None and should_stop():
            break
        try:
            hours = reviews.fetch_player_hours(game.steam_app_id, game.reviews_positive, game.reviews_negative, pages=pages)
            failures = 0
        except RateLimited:
            raise
        except (requests.RequestException, ValueError) as exc:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise SteamError(f"{exc} ({failures} failures in a row, stopping.)") from exc
            continue
        game.player_hours = hours.percentiles if hours else None
        game.player_hours_sample = hours.sample if hours else 0
        game.player_hours_fetched_at = _now()
        session.commit()
        done += 1
        if on_progress:
            on_progress(i, len(pending), game)
    return done


def prices_due(session: Session, everything: bool = False) -> list[Game]:
    """Games you don't own (wishlist, discovery pool, Family Sharing) whose price is due: your
    wishlist's after WISHLIST_PRICE_HOURS, the others' after OTHER_PRICE_DAYS. Wishlist first."""
    now = _now()
    wished = WishlistItem.steam_app_id.is_not(None)
    query = (
        select(Game)
        .outerjoin(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .outerjoin(WishlistItem, WishlistItem.steam_app_id == Game.steam_app_id)
        .where(Game.type == "game", or_(LibraryEntry.steam_app_id.is_(None), Game.family_shared.is_(True)))
        .order_by(WishlistItem.steam_app_id.is_(None), Game.steam_app_id)
    )
    if not everything:
        query = query.where(
            or_(
                Game.price_fetched_at.is_(None),
                and_(wished, Game.price_fetched_at < now - timedelta(hours=WISHLIST_PRICE_HOURS)),
                and_(not_(wished), Game.price_fetched_at < now - timedelta(days=OTHER_PRICE_DAYS)),
            )
        )
    return list(session.scalars(query))


def sync_prices(
    session: Session,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    everything: bool = False,
) -> int:
    """Refresh the prices `prices_due` lists, store.PRICE_BATCH games per store call. Saves batch
    by batch. Returns the number of games whose price was read."""
    pending = prices_due(session, everything=everything)
    done = 0
    for start in range(0, len(pending), store.PRICE_BATCH):
        if should_stop is not None and should_stop():
            break
        batch = pending[start : start + store.PRICE_BATCH]
        prices = store.fetch_prices([g.steam_app_id for g in batch])
        now = _now()
        for game in batch:
            if game.steam_app_id not in prices:
                continue  # the store didn't answer for it: try again next time
            price = prices[game.steam_app_id]
            if price or game.price != {"free": True}:  # a game known to be free stays free
                game.price = price
            game.price_fetched_at = now
            done += 1
        session.commit()
        if on_progress:
            on_progress(min(start + len(batch), len(pending)), len(pending), batch[-1])
    return done


def about_due(session: Session) -> list[Game]:
    """Games that have their store details but were read before the "About this game" text, the
    languages, the price or the other store details were kept: yours first, most played first,
    then your wishlist, then the rest of the pool. Languages only matter for a game that could be
    recommended, so a game you have played is due only for the others."""
    missing = or_(Game.about_text.is_(None), Game.price_fetched_at.is_(None), Game.store_info.is_(None))
    rows = session.execute(
        select(Game, missing)
        .options(selectinload(Game.library_entry))
        .outerjoin(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .outerjoin(WishlistItem, WishlistItem.steam_app_id == Game.steam_app_id)
        .where(
            Game.type == "game",
            Game.details_fetched_at.is_not(None),
            or_(missing, Game.languages.is_(None)),
        )
        .order_by(
            LibraryEntry.steam_app_id.is_(None),
            WishlistItem.steam_app_id.is_(None),
            LibraryEntry.playtime_total_min.desc().nulls_last(),
        )
    ).all()
    return [g for g, missing in rows if missing or not is_played(g.library_entry, bool(g.family_shared))]


def sync_about(
    session: Session,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Read the "About this game" text, the languages, the price and the other store details for
    the games `about_due` lists (one store call each, nothing else is touched). Saves game by game.
    Returns the number read."""
    pending = about_due(session)
    done = failures = 0
    for i, game in enumerate(pending, start=1):
        if should_stop is not None and should_stop():
            break
        try:
            details = store.fetch_store_details(game.steam_app_id)
            failures = 0
        except RateLimited:
            raise
        except requests.RequestException as exc:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise SteamError(f"{exc} ({failures} failures in a row, stopping.)") from exc
            continue
        game.about_text = details.about_text if details is not None else ""
        game.languages = details.languages if details is not None else {}
        game.price, game.price_fetched_at = (details.price if details is not None else {}), _now()
        game.store_info = details.info if details is not None else {}
        session.commit()
        done += 1
        if on_progress:
            on_progress(i, len(pending), game)
    return done


def tags_due(session: Session, everything: bool = False, refresh_days: int = TAGS_REFRESH_DAYS) -> list[Game]:
    """Every game whose tags a sync should read from Steam: never read, read from SteamSpy before
    ShelfLife switched to Steam's (STEAM_TAGS_SINCE), or read more than `refresh_days` ago.
    `everything` lists them all. Games set aside by a search count too: their tags decide whether
    a later search keeps them."""
    query = select(Game).order_by(Game.tags_fetched_at.asc().nulls_first(), Game.steam_app_id)
    if not everything:
        cutoff = max(_now() - timedelta(days=refresh_days), STEAM_TAGS_SINCE)
        query = query.where(or_(Game.tags_fetched_at.is_(None), Game.tags_fetched_at < cutoff))
    return list(session.scalars(query))


def sync_tags(
    session: Session,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    everything: bool = False,
) -> int:
    """Read the tags of the games `tags_due` lists from Steam, store.TAGS_BATCH games a call. A game
    Steam has none for keeps the tags it has, named as Steam names them; one with none at all (and
    that may be a game) gets SteamSpy's or the store page's. Saved batch by batch. Returns the number
    of games read."""
    pending = tags_due(session, everything=everything)
    done = 0
    for start in range(0, len(pending), store.TAGS_BATCH):
        if should_stop is not None and should_stop():
            break
        chunk = pending[start : start + store.TAGS_BATCH]
        found = store.fetch_steam_tags([g.steam_app_id for g in chunk])
        for game in chunk:
            if tags := found.get(game.steam_app_id):
                game.tags = tags
            elif game.tags:
                game.tags = store.steam_names(game.tags)
            elif game.type in (None, "game"):
                game.tags = _fallback_tags(game) or game.tags
            game.tags_fetched_at = _now()
        session.commit()
        done += len(chunk)
        if on_progress:
            on_progress(done, len(pending), chunk[-1])
    return done


def backfill_empty_tags(
    session: Session,
    on_progress: Callable[[int, int, Game], None] | None = None,
) -> int:
    """Retry tags for already-enriched games that ended up with none.

    Uses only the store page, since SteamSpy already came up empty for these.
    """
    empty = [g for g in session.scalars(select(Game)) if not g.tags and g.type != "unavailable"]
    filled = 0
    for i, game in enumerate(empty, start=1):
        try:
            tags = store.fetch_store_page_tags(game.steam_app_id)
        except requests.RequestException:
            continue
        if tags:
            game.tags = tags
            game.tags_fetched_at = _now()
            session.commit()
            filled += 1
        if on_progress:
            on_progress(i, len(empty), game)
    return filled


def _wanted_games():
    """Games in your library or on your wishlist (a wishlist game needs its tags and reviews to be
    ranked, even though you do not own it). Library games come first, most played first."""
    return (
        select(Game)
        .outerjoin(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .outerjoin(WishlistItem, WishlistItem.steam_app_id == Game.steam_app_id)
        .where(or_(LibraryEntry.steam_app_id.is_not(None), WishlistItem.steam_app_id.is_not(None)))
    )


def details_due(
    session: Session,
    everything: bool = False,
    retry_days: int = DETAILS_RETRY_DAYS,
    refresh_days: int = DETAILS_REFRESH_DAYS,
    refresh_batch: int = DETAILS_REFRESH_BATCH,
) -> list[Game]:
    """The library and wishlist games whose store details and tags a sync should fetch, in this order:
    games never fetched (most-played first); games that came back empty (no store page, or a
    game with no tags) more than `retry_days` ago; and the `refresh_batch` oldest games fetched more
    than `refresh_days` ago. `everything` lists every library and wishlist game."""
    wanted = _wanted_games()
    new = list(
        session.scalars(
            wanted.where(Game.details_fetched_at.is_(None)).order_by(LibraryEntry.playtime_total_min.desc().nulls_last())
        )
    )
    fetched = list(
        session.scalars(wanted.where(Game.details_fetched_at.is_not(None)).order_by(Game.details_fetched_at.asc()))
    )
    if everything:
        return new + fetched
    now = _now()
    retry_cutoff, refresh_cutoff = now - timedelta(days=retry_days), now - timedelta(days=refresh_days)
    retry = [
        g
        for g in fetched
        if (g.type == "unavailable" or (g.type == "game" and not g.tags)) and g.details_fetched_at < retry_cutoff
    ]
    retry_ids = {g.steam_app_id for g in retry}
    stale = [g for g in fetched if g.details_fetched_at < refresh_cutoff and g.steam_app_id not in retry_ids]
    return new + retry + stale[:refresh_batch]


def enrich_games(
    session: Session,
    limit: int | None = None,
    on_progress: Callable[[int, int, Game], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    refresh: bool = False,
    everything: bool = False,
) -> int:
    """Fetch store details + tags for games we haven't enriched yet.

    Most-played games go first. Each game is committed on its own, so the run
    can be interrupted or rate-limited and simply resumed later. With `refresh` it
    also retries games that came back empty and refreshes a batch of the oldest
    (`details_due`); `everything` redoes every library game. Returns the number of
    games enriched.
    """
    if refresh or everything:
        pending = details_due(session, everything=everything)
    else:
        pending = list(
            session.scalars(
                _wanted_games()
                .where(Game.details_fetched_at.is_(None))
                .order_by(LibraryEntry.playtime_total_min.desc().nulls_last())
            )
        )
    if limit is not None:
        pending = pending[:limit]

    done = 0
    for i, game in enumerate(pending, start=1):
        if should_stop is not None and should_stop():
            break
        try:
            enrich_one(game)
        except (RateLimited, requests.RequestException):
            session.rollback()
            raise
        session.commit()
        done += 1
        if on_progress:
            on_progress(i, len(pending), game)
    return done
