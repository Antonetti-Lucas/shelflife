"""Save the Steam library to the database and enrich it with store data."""

from datetime import datetime, timedelta, timezone
from typing import Callable

import requests
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from shelflife import store
from shelflife.achievements import AchievementsUnavailable, AchievementStats, fetch_achievements
from shelflife.models import Game, LibraryEntry, PlaytimeSnapshot, WishlistItem
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
        game.family_shared = False  # you own it now, if it was ever a family game

        entry = session.get(LibraryEntry, og.app_id)
        if entry is None:
            entry = LibraryEntry(steam_app_id=og.app_id)
            session.add(entry)
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
DETAILS_RETRY_DAYS = 7  # a game the store had no page for, or no tags for: try again
DETAILS_REFRESH_DAYS = 90  # tags and descriptions drift; refreshed a few at a time
DETAILS_REFRESH_BATCH = 30  # at most this many stale games per sync (about a minute and a half)
REVIEWS_REFRESH_DAYS = 30  # review counts grow
RECENT_REREAD_HOURS = 6  # a game played lately is read again, but not minutes after it was read


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
    conditions = [played_or_family]
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


def _fill_tags(game: Game, spy: store.SpyDetails | None = None) -> None:
    """SteamSpy first; if it has no tags, fall back to the store page's tag votes.

    Tags are best effort: a failure here shouldn't lose the store data. SteamSpy
    also supplies review counts when we don't have any yet. Pass `spy` when its
    details were already fetched, to avoid asking twice.
    """
    if spy is None:
        try:
            spy = store.fetch_spy_details(game.steam_app_id)
        except RateLimited:
            raise
        except (requests.RequestException, ValueError):
            spy = None

    if spy is not None:
        if spy.tags:
            game.tags = spy.tags
        if game.reviews_fetched_at is None and spy.positive is not None:
            game.reviews_positive = spy.positive
            game.reviews_negative = spy.negative or 0
            game.reviews_fetched_at = _now()

    if not game.tags:
        try:
            game.tags = store.fetch_store_page_tags(game.steam_app_id)
        except RateLimited:
            raise
        except (requests.RequestException, ValueError):
            pass
    game.tags_fetched_at = _now()


def enrich_one(game: Game, spy: store.SpyDetails | None = None) -> None:
    """Fetch store details and tags for one game (no commit). `spy`: SteamSpy details
    already in hand."""
    details = store.fetch_store_details(game.steam_app_id)
    if details is None:
        if not game.type:  # a game that was fine before keeps what it had: the store may just have hiccuped
            game.type = "unavailable"
    else:
        if details.name and (not game.name or game.name.startswith("App ")):
            game.name = details.name  # family imports start with a placeholder name
        game.type = details.type
        game.short_description = details.short_description
        game.genres = details.genres
        game.release_date = details.release_date
        game.cover_url = details.cover_url
    game.details_fetched_at = _now()
    _fill_tags(game, spy)


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
    games never fetched (most-played first); games that came back empty (no store page, or
    no tags) more than `retry_days` ago; and the `refresh_batch` oldest games fetched more
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
    retry = [g for g in fetched if (g.type == "unavailable" or not g.tags) and g.details_fetched_at < retry_cutoff]
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
