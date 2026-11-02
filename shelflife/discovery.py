"""Find well-reviewed games you don't own yet, by tag.

How a search works:

1. Steam lists the games under a tag with their review counts and their own tags
   (a few requests per tag; see store.fetch_steam_tag_games). When Steam doesn't know
   the tag or can't be reached, SteamSpy's list is used instead, which has no tags.
   Either list is loose: it includes games that have the tag with only a few votes,
   so a game "tagged Stealth" may be a pizza game.
2. Games you already have and games with too few reviews are dropped; the rest are
   ranked by the lower bound of their positive-review share, so with equal praise
   more reviews rank higher (97% of 20,000 beats 98% of 300).
3. Going down that ranking (tags take turns), a game is kept only if the searched
   tag is among its RELEVANT_TOP most voted ones. Steam's list brings each game's
   tags; for SteamSpy's they are fetched one game at a time, and the games set aside
   are remembered as "off-topic", with their tags, so they are not fetched again
   (and still count if a later search is about them).
4. Kept games get their store details fetched, then they can be scored.
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife import store
from shelflife.models import OFF_TOPIC, Game, LibraryEntry, WishlistItem
from shelflife.quality import wilson_lower_bound
from shelflife.store import Listing
from shelflife.sync import enrich_one
from shelflife.similar import MIN_SIMILARITY, similarity
from shelflife.tagfilter import has_all, has_any
from shelflife.throttle import RateLimited

MIN_REVIEWS = 300  # skip games too obscure for their review score to mean anything
RELEVANT_TOP = 10  # a search tag must be among a game's 10 most voted tags
MAX_CHECKS_PER_GAME = 8  # look at no more than this many games per game wanted
MAX_FAILURES_IN_A_ROW = 3  # stop when the network keeps failing
POPULAR_PAGES = 3  # at most this many pages (1,000 games each, a minute apart) for a search without tags


def rank_candidates(
    listings: Iterable[Listing], skip: set[int], min_reviews: int = MIN_REVIEWS
) -> list[Listing]:
    """The listed games worth looking at, best reviewed first."""
    scored = [
        (wilson_lower_bound(g.positive, g.negative), g)
        for g in listings
        if g.app_id not in skip and g.positive + g.negative >= min_reviews
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [g for _, g in scored]


def turns(ranked: dict[str, list[Listing]]) -> Iterator[tuple[str, Listing]]:
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
    """Pool games whose store details and tags have not been fetched yet. Wishlist games
    are not pool games: a sync fetches those."""
    return list(
        session.scalars(
            select(Game)
            .outerjoin(LibraryEntry)
            .outerjoin(WishlistItem, WishlistItem.steam_app_id == Game.steam_app_id)
            .where(
                LibraryEntry.steam_app_id.is_(None),
                WishlistItem.steam_app_id.is_(None),
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
    not_games: int = 0  # fetched, but the store says it is not a game (DLC, software, ...)
    empty_tags: list[str] = field(default_factory=list)  # tags no games were found for
    stopped: bool = False  # cancelled before finishing
    aborted: str | None = None  # why it gave up early (rate limit, network)


def _add_by_tags(
    session: Session,
    tags: list[str],
    count: int,
    min_reviews: int,
    report: Callable[[str, int, int, str], None],
    stop: Callable[[], bool],
    result: FindResult,
    like: tuple[dict[str, float], dict[str, float]] | None,
    exclude: list[str] | None = None,
) -> bool:
    """Look through the tags' games and keep the ones really about them. False if it
    ended before choosing anything (stopped, or neither Steam nor SteamSpy could be reached)."""
    by_tag: dict[str, list[Listing]] = {}
    try:
        for i, tag in enumerate(tags):
            if stop():
                result.stopped = True
                return False
            report("lists", i, len(tags), f"Looking up “{tag}” on Steam")
            by_tag[tag] = _tag_games(tag, min_reviews, lambda: report("lists", i, len(tags), f"Looking up “{tag}” on SteamSpy"))
            if not by_tag[tag]:
                result.empty_tags.append(tag)
    except RateLimited as exc:
        result.aborted = str(exc)
        return False
    except (requests.RequestException, ValueError) as exc:
        result.aborted = f"Could not reach Steam or SteamSpy: {exc}"
        return False

    skip = known_ids(session)
    ranked = {tag: rank_candidates(listings, skip, min_reviews) for tag, listings in by_tag.items()}
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
            if listing.tags is not None:  # Steam's list brought them
                tags = listing.tags
            elif row is not None and row.type == OFF_TOPIC:  # seen before: its tags are saved
                tags = row.tags or {}
            else:
                fetches += 1
                tags = store.steam_names(store.fetch_spy_details(listing.app_id).tags)
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

        close_enough = like is None or similarity(like[0], tags, like[1]) >= MIN_SIMILARITY
        unwanted = bool(exclude) and has_any(tags, exclude)  # one of the tags you left out
        keep = close_enough and not unwanted and has_all(tags, [tag], RELEVANT_TOP)
        if not keep and row is None and listing.tags is not None:
            result.skipped += 1  # nothing to remember: Steam's list brings its tags again
            continue
        game = row or Game(steam_app_id=listing.app_id, name=listing.name)
        game.reviews_positive, game.reviews_negative = listing.positive, listing.negative
        game.reviews_fetched_at = _now()
        game.tags, game.tags_fetched_at = tags, _now()
        if keep:
            game.type = None  # not fetched yet; kept
            game.details_fetched_at = None
            result.added += 1
        else:
            game.type = OFF_TOPIC
            game.details_fetched_at = _now()  # nothing more to fetch for it
            result.skipped += 1
        if row is None:
            session.add(game)
        session.commit()
    return True


def _tag_games(tag: str, min_reviews: int, on_fallback: Callable[[], None]) -> list[Listing]:
    """The games under a tag: Steam's list, or SteamSpy's when Steam doesn't know the tag or
    can't be reached (`on_fallback` is called first, to say so)."""
    try:
        tag_id = store.steam_tag_id(tag)
        if tag_id is not None:
            return store.fetch_steam_tag_games(tag_id, min_reviews)
    except (RateLimited, requests.RequestException, ValueError):
        pass
    on_fallback()
    return store.fetch_spy_tag_games(tag)


def _add_popular(
    session: Session,
    count: int,
    min_reviews: int,
    report: Callable[[str, int, int, str], None],
    stop: Callable[[], bool],
    result: FindResult,
) -> None:
    """Add the best-reviewed games from SteamSpy's popularity-ordered list, whatever their
    tags. Reads up to POPULAR_PAGES pages (a page a minute), stopping as soon as there are
    enough games you do not already have."""
    skip = known_ids(session)
    found: dict[int, Listing] = {}
    try:
        for page in range(POPULAR_PAGES):
            if stop():
                result.stopped = True
                return
            wait = " (SteamSpy allows one page a minute, so this waits)" if page else ""
            report("lists", page, POPULAR_PAGES, f"Fetching SteamSpy's popular games, page {page + 1}{wait}")
            for g in rank_candidates(store.fetch_spy_page(page), skip, min_reviews):
                found.setdefault(g.app_id, g)
            if len(found) >= count:
                break
    except RateLimited as exc:
        result.aborted = str(exc)
        return
    except (requests.RequestException, ValueError) as exc:
        result.aborted = f"Could not reach SteamSpy: {exc}"
        return
    best = rank_candidates(found.values(), set(), min_reviews)[:count]
    now = _now()
    for g in best:
        # A tag search may have set this game aside as off-topic and kept its row. That says
        # nothing against it as a popular game: bring it back into the pool.
        row = session.get(Game, g.app_id)
        if row is None:
            row = Game(steam_app_id=g.app_id, name=g.name)
            session.add(row)
        row.reviews_positive, row.reviews_negative, row.reviews_fetched_at = g.positive, g.negative, now
        row.type = None
        row.details_fetched_at = None
    session.commit()
    result.added = len(best)


def find_games(
    session: Session,
    tags: list[str],
    count: int,
    min_reviews: int = MIN_REVIEWS,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    like: tuple[dict[str, float], dict[str, float]] | None = None,
    popular: bool = False,
    exclude: list[str] | None = None,
) -> FindResult:
    """Add up to `count` new games that are really about the given tags, and fetch
    their store details.

    `like`: (a game's tag vector, the tag rarity table) to also require each new game
    to be at least MIN_SIMILARITY similar to that game ("games like this one").
    `popular`: ignore tags and take the best-reviewed of SteamSpy's most popular games.
    Games kept by a tag search arrive with their tags, so only their store details are fetched.
    `exclude`: tags a kept game must not have among its top tags (set aside like an off-topic game).

    on_progress(phase, done, total, text) reports what it is doing: phase "lists"
    (looking up tags), "check" (reading a game's tags; done = games kept so far),
    "details" (fetching store details), "done". Games left without details by an
    earlier interrupted run are finished too. Everything is committed game by game,
    so stopping loses nothing.
    """
    report = on_progress or (lambda *args: None)
    stop = should_stop or (lambda: False)
    result = FindResult()

    if popular:
        _add_popular(session, count, min_reviews, report, stop, result)
    elif not _add_by_tags(session, tags, count, min_reviews, report, stop, result, like, exclude):
        return result

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
            enrich_one(game)
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
        if game.type != "game":
            result.not_games += 1
    report("done", result.enriched, len(pending), "")
    return result
