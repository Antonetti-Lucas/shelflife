"""Find well-reviewed games you don't own yet, by tag.

How a search works:

1. SteamSpy lists every game under a tag with its review counts (one request per
   tag). That list is loose: it includes any game that has the tag at all, even
   with a handful of votes, so a game "tagged Stealth" may be a pizza game.
2. Games you already have and games with too few reviews are dropped; the rest are
   ranked by the lower bound of their positive-review share, so with equal praise
   more reviews rank higher (97% of 20,000 beats 98% of 300).
3. Going down that ranking (tags take turns), each game's own tag votes are
   fetched and the game is kept only if the searched tag is among its RELEVANT_TOP
   most voted ones. The others are remembered as "off-topic", with their tags, so
   they are not fetched again (and still count if a later search is about them).
4. Kept games get their store details fetched, then they can be scored.
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife import store
from shelflife.models import Game, LibraryEntry
from shelflife.quality import wilson_lower_bound
from shelflife.store import SpyDetails, SpyListing
from shelflife.sync import enrich_one
from shelflife.similar import MIN_SIMILARITY, similarity
from shelflife.tagfilter import has_all
from shelflife.throttle import RateLimited

MIN_REVIEWS = 300  # skip games too obscure for their review score to mean anything
RELEVANT_TOP = 10  # a search tag must be among a game's 10 most voted tags
MAX_CHECKS_PER_GAME = 8  # look at no more than this many games per game wanted
MAX_FAILURES_IN_A_ROW = 3  # stop when the network keeps failing
OFF_TOPIC = "off-topic"  # Game.type of a game that is listed under a tag but is not about it


def rank_candidates(
    listings: Iterable[SpyListing], skip: set[int], min_reviews: int = MIN_REVIEWS
) -> list[SpyListing]:
    """The listed games worth looking at, best reviewed first."""
    scored = [
        (wilson_lower_bound(g.positive, g.negative), g)
        for g in listings
        if g.app_id not in skip and g.positive + g.negative >= min_reviews
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [g for _, g in scored]


def turns(ranked: dict[str, list[SpyListing]]) -> Iterator[tuple[str, SpyListing]]:
    """Candidates one tag at a time in turn (best first within a tag), so each tag
    gets a fair share. A game listed under two tags comes up once."""
    position = {tag: 0 for tag in ranked}
    seen: set[int] = set()
    while True:
        progressed = False
        for tag, games in ranked.items():
            i = position[tag]
            while i < len(games) and games[i].app_id in seen:
                i += 1
            if i < len(games):
                seen.add(games[i].app_id)
                position[tag] = i + 1
                progressed = True
                yield tag, games[i]
            else:
                position[tag] = i
        if not progressed:
            return


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def known_ids(session: Session) -> set[int]:
    """Games a search has nothing to do for: owned, family, or already in the pool.
    Off-topic games are not here: they are looked at again, from their saved tags."""
    return set(session.scalars(select(Game.steam_app_id).where(Game.type.is_distinct_from(OFF_TOPIC))))


def pending_pool(session: Session) -> list[Game]:
    """Pool games whose store details and tags have not been fetched yet."""
    return list(
        session.scalars(
            select(Game)
            .outerjoin(LibraryEntry)
            .where(
                LibraryEntry.steam_app_id.is_(None),
                Game.details_fetched_at.is_(None),
            )
        )
    )


@dataclass
class FindResult:
    added: int = 0  # new games kept
    enriched: int = 0  # games whose store details were fetched
    checked: int = 0  # games whose own tags were looked at
    skipped: int = 0  # of those, how many only mention the tag in passing
    failed: int = 0  # games skipped after a network error
    empty_tags: list[str] = field(default_factory=list)  # tags SteamSpy knows no games for
    stopped: bool = False  # cancelled before finishing
    aborted: str | None = None  # why it gave up early (rate limit, network)


def find_games(
    session: Session,
    tags: list[str],
    count: int,
    min_reviews: int = MIN_REVIEWS,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    like: tuple[dict[str, float], dict[str, float]] | None = None,
) -> FindResult:
    """Add up to `count` new games that are really about the given tags, and fetch
    their store details.

    `like`: (a game's tag vector, the tag rarity table) to also require each new game
    to be at least MIN_SIMILARITY similar to that game ("games like this one").

    on_progress(phase, done, total, text) reports what it is doing: phase "lists"
    (looking up tags), "check" (reading a game's tags; done = games kept so far),
    "details" (fetching store details), "done". Games left without details by an
    earlier interrupted run are finished too. Everything is committed game by game,
    so stopping loses nothing.
    """
    report = on_progress or (lambda *args: None)
    stop = should_stop or (lambda: False)
    result = FindResult()

    by_tag: dict[str, list[SpyListing]] = {}
    try:
        for i, tag in enumerate(tags):
            if stop():
                result.stopped = True
                return result
            report("lists", i, len(tags), f"Looking up “{tag}” on SteamSpy")
            by_tag[tag] = store.fetch_spy_tag_games(tag)
            if not by_tag[tag]:
                result.empty_tags.append(tag)
    except RateLimited as exc:
        result.aborted = str(exc)
        return result
    except (requests.RequestException, ValueError) as exc:
        result.aborted = f"Could not reach SteamSpy: {exc}"
        return result

    skip = known_ids(session)
    ranked = {tag: rank_candidates(listings, skip, min_reviews) for tag, listings in by_tag.items()}
    spies: dict[int, SpyDetails] = {}
    fetches = failures_in_a_row = 0
    for tag, listing in turns(ranked):
        if result.added >= count or fetches >= count * MAX_CHECKS_PER_GAME:
            break
        if stop():
            result.stopped = True
            break
        report("check", result.added, count, f"Checking {listing.name}")

        row = session.get(Game, listing.app_id)
        try:
            if row is not None and row.type == OFF_TOPIC:  # seen before: its tags are saved
                spy = SpyDetails(row.tags or {}, row.reviews_positive, row.reviews_negative)
            else:
                fetches += 1
                spy = store.fetch_spy_details(listing.app_id)
        except RateLimited as exc:
            result.aborted = str(exc)
            break
        except (requests.RequestException, ValueError):
            result.failed += 1
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                result.aborted = "The network keeps failing; try again later."
                break
            continue
        failures_in_a_row = 0
        result.checked += 1

        game = row or Game(steam_app_id=listing.app_id, name=listing.name)
        game.reviews_positive, game.reviews_negative = listing.positive, listing.negative
        game.reviews_fetched_at = _now()
        game.tags = spy.tags
        close_enough = like is None or similarity(like[0], spy.tags, like[1]) >= MIN_SIMILARITY
        if close_enough and has_all(spy.tags, [tag], RELEVANT_TOP):
            game.type = None  # not fetched yet; kept
            game.details_fetched_at = None
            spies[game.steam_app_id] = spy
            result.added += 1
        else:
            game.type = OFF_TOPIC
            game.details_fetched_at = _now()  # nothing more to fetch for it
            result.skipped += 1
        if row is None:
            session.add(game)
        session.commit()

    pending = pending_pool(session)
    failures_in_a_row = 0
    for i, game in enumerate(pending):
        if result.aborted or result.stopped:
            break
        if stop():
            result.stopped = True
            break
        report("details", i, len(pending), f"Fetching details for {game.name}")
        try:
            enrich_one(game, spy=spies.get(game.steam_app_id))
        except RateLimited as exc:
            session.rollback()
            result.aborted = str(exc)
            break
        except requests.RequestException:
            session.rollback()
            result.failed += 1
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                result.aborted = "The network keeps failing; try again later."
                break
            continue
        session.commit()
        failures_in_a_row = 0
        result.enriched += 1
    report("done", result.enriched, len(pending), "")
    return result
