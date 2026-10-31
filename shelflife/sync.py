"""Save the Steam library to the database and enrich it with store data."""

from datetime import datetime, timezone
from typing import Callable

import requests
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from shelflife import store
from shelflife.achievements import AchievementsUnavailable, AchievementStats, fetch_achievements
from shelflife.models import Game, LibraryEntry, PlaytimeSnapshot
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


def sync_achievements(
    session: Session,
    api_key: str,
    steam_id: str,
    on_progress: Callable[[int, int, Game, AchievementStats], None] | None = None,
) -> int:
    """Fetch achievement progress for played games, most-played first.

    Fetches games never fetched before, games played in the last two weeks
    (their progress has probably changed), and games fetched before unlock
    times were stored. Returns the number fetched.
    """
    pending = list(
        session.scalars(
            select(LibraryEntry)
            .join(Game, Game.steam_app_id == LibraryEntry.steam_app_id)
            .where(
                # Family games have no playtime; achievements show if you played them.
                or_(LibraryEntry.playtime_total_min > 0, Game.family_shared.is_(True)),
                or_(
                    LibraryEntry.achievements_fetched_at.is_(None),
                    LibraryEntry.playtime_recent_min > 0,
                    and_(
                        LibraryEntry.achievements_unlocked > 0,
                        LibraryEntry.unlock_times.is_(None),
                    ),
                ),
            )
            .order_by(LibraryEntry.playtime_total_min.desc())
        )
    )

    done = failures = 0
    for i, entry in enumerate(pending, start=1):
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


def sync_backlog_reviews(
    session: Session,
    max_playtime_min: int,
    on_progress: Callable[[int, int, Game], None] | None = None,
) -> int:
    """Fetch review counts for owned games played under max_playtime_min.

    Only backlog games need them: the quality filter ranks candidates, not games
    you've already played. Returns the number fetched.
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
    done = failures = 0
    for i, game in enumerate(pending, start=1):
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
            on_progress(i, len(pending), game)
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


def enrich_games(
    session: Session,
    limit: int | None = None,
    on_progress: Callable[[int, int, Game], None] | None = None,
) -> int:
    """Fetch store details + tags for games we haven't enriched yet.

    Most-played games go first. Each game is committed on its own, so the run
    can be interrupted or rate-limited and simply resumed later. Returns the
    number of games enriched.
    """
    pending = list(
        session.scalars(
            select(Game)
            .join(LibraryEntry)
            .where(Game.details_fetched_at.is_(None))
            .order_by(LibraryEntry.playtime_total_min.desc())
        )
    )
    if limit is not None:
        pending = pending[:limit]

    done = 0
    for i, game in enumerate(pending, start=1):
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
