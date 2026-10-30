"""Build a pool of well-reviewed games you don't own, from your favorite tags."""

from datetime import datetime, timezone
from typing import Callable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife import store
from shelflife.models import Game, LibraryEntry
from shelflife.quality import wilson_lower_bound
from shelflife.sync import enrich_one
from shelflife.throttle import RateLimited

MIN_REVIEWS = 300  # skip games too obscure for their review score to mean anything


def build_pool(session: Session, tags: list[str], per_tag: int = 40) -> int:
    """Add the best-reviewed unowned games for each tag as bare Game rows.

    SteamSpy's tag lists include review counts, so no extra calls are needed to
    rank them. Returns the number of new games added.
    """
    owned = set(session.scalars(select(LibraryEntry.steam_app_id)))
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    added = 0
    for tag in tags:
        listings = store.fetch_spy_tag_list(tag)
        reviewed = [
            (wilson_lower_bound(g.positive, g.negative), g)
            for g in listings
            if g.app_id not in owned and g.positive + g.negative >= MIN_REVIEWS
        ]
        reviewed.sort(key=lambda pair: pair[0], reverse=True)
        for _, g in reviewed[:per_tag]:
            if session.get(Game, g.app_id) is not None:
                continue
            session.add(
                Game(
                    steam_app_id=g.app_id,
                    name=g.name,
                    reviews_positive=g.positive,
                    reviews_negative=g.negative,
                    reviews_fetched_at=now,
                )
            )
            added += 1
        session.commit()
    return added


def enrich_pool(
    session: Session,
    limit: int | None = None,
    on_progress: Callable[[int, int, Game], None] | None = None,
) -> int:
    """Fetch store details + tags for pool games. Resumable, one commit per game."""
    pending = list(
        session.scalars(
            select(Game)
            .outerjoin(LibraryEntry)
            .where(LibraryEntry.steam_app_id.is_(None), Game.details_fetched_at.is_(None))
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
