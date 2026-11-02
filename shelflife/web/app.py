"""Local web app: recommendations, your library, exploring Steam, game pages and your diary.

Runs on your own machine only (see scripts/web.py). There is no login: anyone who
can reach the port can change your diary, so it binds to 127.0.0.1, answers only requests
addressed to this computer's own names, and refuses form posts from other sites.
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from shelflife import diary
from shelflife.diary import favorite_ids
from shelflife.config import try_load_settings
from shelflife.db import SessionLocal, init_db
from shelflife.hours import set_hours
from shelflife.models import DiaryEntry, Game, LibraryEntry, WishlistItem
from shelflife.quality import quality_factor
from shelflife.recency import recency
from shelflife.discover_job import SECONDS_PER_GAME, job as discover_job
from shelflife.discovery import MIN_REVIEWS
from shelflife.recommend import explain, play_score_of, recommend, score_all, taste_profile, top_taste_tags
from shelflife.score import engagement_score
from shelflife.settings import TASTE_MODE_LABELS, TASTE_MODES, get_hide_adult, get_taste_mode, set_hide_adult, set_taste_mode
from shelflife.learned import text_index
from shelflife.similar import search_tags, similar_to, tag_idf
from shelflife.stats import compute_stats
from shelflife.sync_job import STEPS as SYNC_STEPS
from shelflife.sync_job import job as sync_job
from shelflife.sync_job import what_is_due
from shelflife.tagfilter import TagIndex, parse_tags, split_excluded
from shelflife.tags import is_adult
from shelflife.web.lists import ListFilter, paginate, recommended, whole
from shelflife.wishlist import order_key, rank_label

BASE = Path(__file__).parent
PAGE_SIZE = 40
RATING_OPTIONS = [r / 2 for r in range(1, 11)]  # 0.5 ... 5.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ShelfLife", lifespan=lifespan)

# The names this app answers to. Binding to 127.0.0.1 is not enough on its own: a web page can
# point a name it controls at 127.0.0.1 ("DNS rebinding"), and your browser would then let that
# page read and change your diary, because to the browser it is the page's own site. Such a
# request still carries the page's name in its Host header, so anything not on this list is
# refused. SHELFLIFE_ALLOWED_HOSTS (comma separated) adds names, e.g. a private network address.
ALLOWED_HOSTS = [h.strip() for h in os.getenv("SHELFLIFE_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def get_session():
    with SessionLocal() as session:
        yield session


@app.middleware("http")
async def block_cross_site_posts(request: Request, call_next):
    """Stop other websites from submitting forms to this local app."""
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request blocked.", status_code=403)
    return await call_next(request)


# ---- helpers used by the templates ----------------------------------------


def reviews_text(game: Game) -> str:
    pos, neg = game.reviews_positive, game.reviews_negative
    if pos is None or neg is None or pos + neg == 0:
        return "no review data"
    return f"{pos / (pos + neg):.0%} positive · {pos + neg:,} reviews"


def rating_text(rating: float | None) -> str:
    return "—" if rating is None else f"{rating:g}"


def source_label(game: Game) -> str:
    if game.library_entry is None:
        return "Not owned"
    return "Family" if game.family_shared else "Owned"


def short_reviews(game: Game) -> str:
    """"95% positive · 322k reviews", for a card."""
    pos, neg = game.reviews_positive, game.reviews_negative
    if pos is None or neg is None or pos + neg == 0:
        return "no reviews yet"
    total = pos + neg
    count = f"{total / 1_000_000:.1f}M" if total >= 1_000_000 else f"{total / 1000:.0f}k" if total >= 10_000 else f"{total:,}"
    return f"{pos / total:.0%} positive · {count} reviews"


def stars(rating: float) -> str:
    """4.5 as ★★★★½."""
    return "★" * int(rating) + ("½" if rating % 1 else "")


def fit_level(similarity: float) -> str:
    """How a match badge is coloured."""
    return "high" if similarity >= 0.25 else "low" if similarity < 0 else "mid"


def game_of(label: str) -> str:
    """The game's name from a reason such as "Hades (rated 5, 66% similar)"."""
    return label.rsplit(" (", 1)[0]


templates.env.globals.update(
    RATING_OPTIONS=RATING_OPTIONS,
    STATUSES=diary.STATUSES,
    STATUS_LABELS=diary.STATUS_LABELS,
    reviews_text=reviews_text,
    short_reviews=short_reviews,
    rating_text=rating_text,
    source_label=source_label,
    stars=stars,
    fit_level=fit_level,
    game_of=game_of,
)


def safe_next(url: str | None, default: str) -> str:
    """Only redirect to paths on this site."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return default


# ---- pages ------------------------------------------------------------------


def wishlist_by_id(session: Session) -> dict[int, WishlistItem]:
    return {w.steam_app_id: w for w in session.scalars(select(WishlistItem))}


def wishlist_note(item: WishlistItem | None, game: Game) -> str | None:
    """The small line shown with a wishlisted game: your rank, when you added it, and whether you
    can already play it."""
    if item is None:
        return None
    bits = ["On your wishlist"]
    if rank_label(item.priority):
        bits.append(f"your rank {rank_label(item.priority)}")
    if item.added_at:
        bits.append(f"added {item.added_at.strftime('%Y-%m-%d')}")
    if game.library_entry is not None:
        bits.append("you can already play it with Family Sharing" if game.family_shared else "you own it")
    return " · ".join(bits)


REC_PAGE_SIZES = (12, 24, 48)  # one list on its own page ("See all")
STRIP_SIZE = 12  # the best picks of each list, side by side on For you
REC_LISTS = {
    "backlog": ("From your backlog", "owned, barely played"),
    "family": ("From your family library", "free to play"),
    "wishlist": ("From your wishlist", "on Steam, not yours yet"),
    "discover": ("To discover", "not in your library"),
}


@app.get("/")
def home(
    request: Request,
    which: str = Query("", alias="list"),
    size: str = "24",
    page: str = "1",
    session: Session = Depends(get_session),
):
    """For you: each list as one row of its best picks, or with `list`, one list in full, paged."""
    size = whole(size, 24)
    size = size if size in REC_PAGE_SIZES else 24
    ratings = diary.load_ratings(session)
    result = recommend(
        session,
        ratings,
        diary.load_not_interested(session),
        limit=100_000,  # everything; the page shows a part
        wishlist_ids=wishlist_by_id(session).keys(),
    )
    lists = {"backlog": result.backlog, "family": result.family, "wishlist": result.wishlist, "discover": result.discover}
    only = which if which in REC_LISTS else None

    sections = {}
    for key, recs in lists.items():
        if only and key != only:
            continue
        part = paginate(recs, page, size) if only else paginate(recs, 1, STRIP_SIZE)
        # "recs", not "items": that clashes with dict.items in Jinja
        sections[key] = {"recs": part.shown, "total": part.total, "page": part.page, "pages": part.pages}

    def page_url(key: str, page: int | None = None) -> str:
        """Link to one list on its own page (the strips link here with "See all")."""
        params = {"list": key} | ({"size": size} if size != 24 else {}) | ({"page": page} if page and page > 1 else {})
        return f"/?{urlencode(params)}"

    here = page_url(only, sections[only]["page"]) if only else "/"
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {
            "taste": result.taste,
            "sections": sections,
            "lists": REC_LISTS,
            "only": only,
            "here": here,
            "page_url": page_url,
            "size": size,
            "sizes": REC_PAGE_SIZES,
            "ratings": ratings,
            "taste_mode": get_taste_mode(session),
        },
    )


EXPLORE_PAGE_SIZE = 24
EXPLORE_WISHLIST = ("all", "only", "not")  # any game, only wishlisted ones, or none of them


def current_url(request: Request) -> str:
    """This page's path and query, to come back to after an action."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


# The Library's sortable columns and the order each starts in when you click it.
LIBRARY_COLUMNS = {
    "name": "asc", "source": "asc", "hours": "desc", "recent": "desc", "score": "desc",
    "fit": "desc", "rating": "desc", "status": "asc",
}


@app.get("/library")
def library(
    request: Request,
    q: str = "",
    source: str = "all",
    sort: str = "",
    dir: str = "",
    rec: str = "all",
    status: str = "all",
    tag: str = "",
    match: str = "all",
    page: str = "1",
    session: Session = Depends(get_session),
):
    """Your games as a table; click a column's name to sort by it, again to turn the order round."""
    ratings = diary.load_ratings(session)
    entries = diary.load_entries(session)
    filters = ListFilter.read(q, tag, match)
    status = status if status in (*diary.STATUSES, "none") else "all"
    scores = score_all(session, ratings, diary.load_not_interested(session))
    scope = scores.backlog | scores.family  # what "recommended" means in the library
    rec, keep = recommended(rec, {i: scores.by_id[i].score for i in scope})
    sort = sort if sort in LIBRARY_COLUMNS else ("fit" if keep is not None else "hours")
    dir = dir if dir in ("asc", "desc") else LIBRARY_COLUMNS[sort]

    query = (
        select(Game)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(or_(Game.type == "game", Game.type.is_(None)))
    )
    if source == "owned":
        query = query.where(Game.family_shared.is_not(True))
    elif source == "family":
        query = query.where(Game.family_shared.is_(True))

    games = list(session.scalars(query))
    tag_index = TagIndex(g.tags for g in games)  # the whole source, before search and tag filters
    rows = []
    for game in games:
        if (keep is not None and game.steam_app_id not in keep) or not filters.keeps(game):
            continue
        mark = entries.get(game.steam_app_id)
        game_status = mark.status if mark else None
        if status == "none" and game_status is not None:
            continue
        if status in diary.STATUSES and game_status != status:
            continue
        entry = game.library_entry
        fit = scores.by_id[game.steam_app_id].score if game.steam_app_id in scope else None
        rows.append(
            {
                "game": game,
                "hours": entry.playtime_total_hours,
                "score": engagement_score(entry).total,
                "fit": None if fit is None else fit * 100,
                "rating": ratings.get(game.steam_app_id),
                "status": game_status,
                "last_played": entry.last_played,
            }
        )

    value = {
        "name": lambda r: r["game"].name.lower(),
        "source": lambda r: source_label(r["game"]),
        "hours": lambda r: r["hours"] or 0,
        "recent": lambda r: r["last_played"],
        "score": lambda r: r["score"] or 0,
        "fit": lambda r: r["fit"],
        "rating": lambda r: r["rating"],
        "status": lambda r: diary.STATUSES.index(r["status"]) if r["status"] else None,
    }[sort]
    rows.sort(key=lambda r: r["game"].name.lower())  # ties in name order
    known = [r for r in rows if value(r) is not None]
    rows = sorted(known, key=value, reverse=dir == "desc") + [r for r in rows if value(r) is None]  # blanks last

    part = paginate(rows, page, PAGE_SIZE)
    settings = {"q": q, "source": source, "tag": tag, "match": filters.match, "status": status, "rec": rec, "sort": sort, "dir": dir}

    def url(**changes) -> str:
        return "/library?" + urlencode({k: v for k, v in (settings | changes).items() if v not in ("", None)})

    def sort_url(column: str) -> str:
        """Sort by this column; the column already sorted by turns its order round."""
        if column == sort:
            return url(dir="asc" if dir == "desc" else "desc")
        return url(sort=column, dir=LIBRARY_COLUMNS[column])

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "rows": part.shown,
            "total": part.total,
            "source": source,
            "sort": sort,
            "dir": dir,
            "sort_url": sort_url,
            "pager_base": url() + "&page=",
            "rec": rec,
            "status": status,
            **filters.context(tag_index),
            "page": part.page,
            "pages": part.pages,
            "here": current_url(request),
        },
    )


@app.get("/explore")
def explore(
    request: Request,
    q: str = "",
    sort: str = "",
    rec: str = "all",
    tag: str = "",
    match: str = "all",
    like: str = "",
    wishlist: str = "all",
    page: str = "1",
    session: Session = Depends(get_session),
):
    """Games you don't own: the discovery pool, ranked by how well they fit you, or by
    how like one game (`like`) they are."""
    filters = ListFilter.read(q, tag, match)
    wishlist = wishlist if wishlist in EXPLORE_WISHLIST else "all"
    wished = wishlist_by_id(session)
    ratings = diary.load_ratings(session)
    not_interested = diary.load_not_interested(session)
    scores = score_all(session, ratings, not_interested)
    rec, keep = recommended(rec, {i: scores.by_id[i].score for i in scores.discover})

    query = (
        select(Game)
        .outerjoin(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(LibraryEntry.steam_app_id.is_(None), Game.type == "game")
    )
    games = list(session.scalars(query))
    if get_hide_adult(session):
        games = [g for g in games if not is_adult(g.tags)]
    tag_index = TagIndex(g.tags for g in games)
    recs = []
    for game in games:
        rec_item = scores.by_id.get(game.steam_app_id)  # None: no tags yet
        on_wishlist = game.steam_app_id in wished
        if (
            rec_item is None
            or game.steam_app_id in not_interested
            or (keep is not None and game.steam_app_id not in keep)
            or (wishlist == "only" and not on_wishlist)
            or (wishlist == "not" and on_wishlist)
            or not filters.keeps(game)
        ):
            continue
        recs.append(rec_item)

    ref = None  # the game "like this" is about
    if (like_id := parse_app_id(like)) is not None:
        found = session.get(Game, like_id)
        ref = found if found is not None and found.tags else None
    like_tags = []
    if ref is not None:
        idf = tag_idf(session)
        recs = similar_to(ref, [r.game for r in recs], idf, text_index(session))  # same games, ranked by likeness to ref
        like_tags = search_tags(ref, idf)

    keys = {
        "fit": lambda r: -r.score,
        "similar": lambda r: -r.score,
        "reviews": lambda r: -((r.game.reviews_positive or 0) + (r.game.reviews_negative or 0)),
        "name": lambda r: r.game.name.lower(),
    }
    sorts = ("similar", "reviews", "name") if ref is not None else ("fit", "reviews", "name")
    sort = sort if sort in sorts else sorts[0]
    recs.sort(key=keys[sort])

    part = paginate(recs, page, EXPLORE_PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "explore.html",
        {
            "recs": part.shown,
            "total": part.total,
            "sort": sort,
            "sorts": sorts,
            "ref": ref,
            "like_tags": like_tags,
            "rec": rec,
            "wishlist": wishlist,
            "notes": {i: wishlist_note(w, session.get(Game, i)) for i, w in wished.items()},
            **filters.context(tag_index),
            "page": part.page,
            "pages": part.pages,
            "here": current_url(request),
            "ratings": ratings,
            "job": discover_job.snapshot(),
            "sync_running": sync_job.snapshot().running,
            "taste_tags": top_taste_tags(session, ratings, not_interested),
            "find_tag_options": TagIndex(session.scalars(select(Game.tags))).popular(),
            "find_counts": FIND_COUNTS,
            "find_min_reviews": FIND_MIN_REVIEWS,
            "min_reviews_default": MIN_REVIEWS,
            "seconds_per_game": SECONDS_PER_GAME,
        },
    )


FIND_COUNTS = (10, 25, 50, 100)
FIND_MIN_REVIEWS = (100, 300, 1000)
MAX_FIND_TAGS = 6


def parse_app_id(raw: str) -> int | None:
    """A game's app id typed into a form, or None when it is not one (so no 500 for "²" or a
    number too big for the database)."""
    raw = raw.strip()
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else None


def choice(raw: str, allowed: tuple[int, ...], default: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value in allowed else default


@app.post("/explore/find")
def find_more_games(
    tags: str = Form(""),
    count: str = Form("25"),
    min_reviews: str = Form(str(MIN_REVIEWS)),
    like: str = Form(""),
    source: str = Form("tags"),
    session: Session = Depends(get_session),
):
    """Look for new games to explore: starts a background search and returns at once.
    With `like` (a game's app id) it looks for close matches to that game instead; with
    source "popular" it ignores tags and takes the best-reviewed popular games."""
    if sync_job.snapshot().running:
        discover_job.fail("A sync is running. Wait for it to finish, or stop it, then look for games.")
        return RedirectResponse("/explore#find", status_code=303)
    if like.strip():
        like_id = parse_app_id(like)
        ref = session.get(Game, like_id) if like_id is not None else None
        if ref is None or not ref.tags:
            discover_job.fail("That game has no tags yet, so there is nothing to compare.")
            return RedirectResponse("/explore#find", status_code=303)
        discover_job.start(
            search_tags(ref, tag_idf(session)),
            choice(count, FIND_COUNTS, 25),
            choice(min_reviews, FIND_MIN_REVIEWS, MIN_REVIEWS),
            like=(ref.steam_app_id, ref.name),
        )
        return RedirectResponse(f"/explore?like={ref.steam_app_id}#find", status_code=303)

    if source == "popular":
        discover_job.start(
            [],
            choice(count, FIND_COUNTS, 25),
            choice(min_reviews, FIND_MIN_REVIEWS, MIN_REVIEWS),
            popular=True,
        )
        return RedirectResponse("/explore#find", status_code=303)

    wanted, leave_out = split_excluded(parse_tags(tags))  # "-Horror": search, but set aside games with it
    index = TagIndex(session.scalars(select(Game.tags)))
    leave_out = [index.spelling(t) for t in leave_out]
    if wanted:
        wanted = [index.spelling(t) for t in wanted][:MAX_FIND_TAGS]
    else:  # no tags to search for: the tags you like most, minus any you left out
        taste = top_taste_tags(session, diary.load_ratings(session), diary.load_not_interested(session))
        wanted = [t for t in taste if t.lower() not in {x.lower() for x in leave_out}]
    if not wanted:
        discover_job.fail("Rate a few games first so there are taste tags to search for, or type some tags.")
    else:
        discover_job.start(
            wanted,
            choice(count, FIND_COUNTS, 25),
            choice(min_reviews, FIND_MIN_REVIEWS, MIN_REVIEWS),
            exclude=leave_out,
        )
    return RedirectResponse("/explore#find", status_code=303)


WISHLIST_SORTS = {
    "fit": "Best fit",
    "priority": "Your order",
    "added": "Newest added",
    "reviews": "Most reviews",
    "name": "Name",
}


@app.get("/wishlist")
def wishlist_page(
    request: Request,
    q: str = "",
    sort: str = "fit",
    rec: str = "all",
    tag: str = "",
    match: str = "all",
    page: str = "1",
    session: Session = Depends(get_session),
):
    """Your Steam wishlist, ranked by how well each game fits your taste."""
    filters = ListFilter.read(q, tag, match)
    sort = sort if sort in WISHLIST_SORTS else "fit"
    items = wishlist_by_id(session)
    games = {g.steam_app_id: g for g in session.scalars(select(Game).where(Game.steam_app_id.in_(list(items))))} if items else {}
    ratings = diary.load_ratings(session)
    not_interested = diary.load_not_interested(session)
    scores = score_all(session, ratings, not_interested)

    ranked, waiting, other, dismissed = [], 0, 0, 0
    for app_id, game in games.items():
        if app_id in not_interested:
            dismissed += 1
        elif app_id in scores.by_id:
            ranked.append(scores.by_id[app_id])
        elif game.details_fetched_at is None:
            waiting += 1  # its store details have not been fetched yet
        else:
            other += 1  # DLC or software, or no tags to rank it by

    rec, keep = recommended(rec, {r.game.steam_app_id: r.score for r in ranked})
    tag_index = TagIndex(r.game.tags for r in ranked)
    recs = [r for r in ranked if (keep is None or r.game.steam_app_id in keep) and filters.keeps(r.game)]
    keys = {
        "fit": lambda r: -r.score,
        "priority": lambda r: order_key(items[r.game.steam_app_id]),
        "added": lambda r: -(items[r.game.steam_app_id].added_at.timestamp() if items[r.game.steam_app_id].added_at else 0),
        "reviews": lambda r: -((r.game.reviews_positive or 0) + (r.game.reviews_negative or 0)),
        "name": lambda r: r.game.name.lower(),
    }
    recs.sort(key=keys[sort])

    part = paginate(recs, page, EXPLORE_PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "wishlist.html",
        {
            "recs": part.shown,
            "notes": {r.game.steam_app_id: wishlist_note(items[r.game.steam_app_id], r.game) for r in part.shown},
            "total": part.total,
            "on_list": len(items),
            "waiting": waiting,
            "other": other,
            "dismissed": dismissed,
            "sort": sort,
            "sorts": WISHLIST_SORTS,
            "rec": rec,
            **filters.context(tag_index),
            "page": part.page,
            "pages": part.pages,
            "here": current_url(request),
            "ratings": ratings,
        },
    )


@app.get("/sync")
def sync_page(request: Request, session: Session = Depends(get_session)):
    job = sync_job.snapshot()
    return templates.TemplateResponse(
        request,
        "sync.html",
        {
            "job": job,
            "configured": try_load_settings() is not None,
            "finding": discover_job.snapshot().running,
            "due": what_is_due(session, with_due=not job.running),
            "steps": SYNC_STEPS,
        },
    )


@app.post("/sync/start")
def sync_start(full: str = Form("")):
    """Bring your library up to date: starts the sync in the background and returns at once.
    With full=1 it re-reads everything instead of only what is due."""
    settings = try_load_settings()
    if discover_job.snapshot().running:
        sync_job.fail("Finding games is running. Wait for it to finish, or stop it, then sync.")
    elif settings is None:
        sync_job.fail(
            "Sync needs STEAM_API_KEY and STEAM_ID in your .env file (see Setup in the README). "
            "Add them and restart the app."
        )
    else:
        sync_job.start(settings.steam_api_key, settings.steam_id, full=full == "1")
    return RedirectResponse("/sync", status_code=303)


@app.post("/sync/stop")
def sync_stop():
    sync_job.cancel()
    return RedirectResponse("/sync", status_code=303)


@app.post("/explore/find/stop")
def stop_finding_games():
    discover_job.cancel()
    return RedirectResponse("/explore#find", status_code=303)


@app.get("/game/{app_id}")
def game_page(request: Request, app_id: int, session: Session = Depends(get_session)):
    game = session.get(Game, app_id)
    if game is None:
        raise HTTPException(404, "Game not found")
    entry = game.library_entry
    tags = sorted(game.tags.items(), key=lambda kv: kv[1], reverse=True)[:15] if game.tags else []
    top_votes = tags[0][1] if tags else 1
    ratings = diary.load_ratings(session)
    not_interested = diary.load_not_interested(session)
    return templates.TemplateResponse(
        request,
        "game.html",
        {
            "game": game,
            "entry": entry,
            "tags": [(name, votes / top_votes) for name, votes in tags],
            "breakdown": engagement_score(entry) if entry else None,
            "recency": recency(entry) if entry else None,
            "quality": quality_factor(game.reviews_positive, game.reviews_negative),
            "diary": diary.get_entry(session, app_id),
            "match": explain(session, ratings, not_interested, app_id),
            "like_tags": search_tags(game, tag_idf(session)) if game.tags else [],
            "find_counts": FIND_COUNTS,
            "wish_note": wishlist_note(session.get(WishlistItem, app_id), game),
            "play": play_score_of(session, app_id),
            "taste_mode": get_taste_mode(session),
        },
    )


RATINGS_PAGE_SIZE = 30  # the list view, where each game can be edited
POSTERS_PAGE_SIZE = 72  # the poster grid
RATINGS_SORTS = {"rating": "Highest rated", "rated": "Recently rated", "release": "Release date", "name": "Name"}
YEAR = re.compile(r"\b(19[5-9]\d|20\d\d)\b")


def release_year(game: Game) -> int | None:
    """The year in Steam's release date text ("17 Sep, 2020", "Nov 16, 2009", "Q1 2027"...)."""
    found = YEAR.search(game.release_date or "")
    return int(found.group(1)) if found else None


def poster_url(game: Game) -> str:
    """Steam's portrait library art (600x900), from the same store CDN as the covers. Some games
    have none; the page then falls back to the wide cover."""
    return f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{game.steam_app_id}/library_600x900.jpg"


@app.get("/ratings")
def ratings_page(
    request: Request,
    view: str = "grid",
    rating: str = "",
    decade: str = "",
    tag: str = "",
    sort: str = "rating",
    page: str = "1",
    session: Session = Depends(get_session),
):
    """Every game you rated or reviewed: a poster wall to browse, or a list to edit."""
    view = "list" if view == "list" else "grid"
    sort = sort if sort in RATINGS_SORTS else "rating"
    entries = session.execute(select(DiaryEntry, Game).join(Game, Game.steam_app_id == DiaryEntry.steam_app_id)).all()
    # A game can have just a review and no rating, so list those too.
    rated = [(e, g) for e, g in entries if e.rating is not None or e.review]
    decades = sorted({y // 10 * 10 for _, g in rated if (y := release_year(g))}, reverse=True)
    tags = ListFilter.read("", tag, "all")  # a minus leaves a tag out, as on the other pages
    tag_index = TagIndex(g.tags for _, g in rated)

    shown = rated
    if rating in {f"{o:g}" for o in RATING_OPTIONS}:
        shown = [(e, g) for e, g in shown if e.rating == float(rating)]
    elif rating == "none":
        shown = [(e, g) for e, g in shown if e.rating is None]
    else:
        rating = ""
    if (wanted_decade := whole(decade, 0)) in decades:
        shown = [(e, g) for e, g in shown if (y := release_year(g)) and y // 10 * 10 == wanted_decade]
    else:
        decade = ""
    shown = [(e, g) for e, g in shown if tags.keeps(g)]

    shown.sort(key=lambda p: p[1].name.lower())
    if sort == "rating":
        shown.sort(key=lambda p: -(p[0].rating or 0))
    elif sort == "rated":
        shown.sort(key=lambda p: p[0].updated_at, reverse=True)
    elif sort == "release":
        shown.sort(key=lambda p: (release_year(p[1]) is None, -(release_year(p[1]) or 0)))
    part = paginate(shown, page, RATINGS_PAGE_SIZE if view == "list" else POSTERS_PAGE_SIZE)

    settings = {"view": view, "rating": rating, "decade": decade, "tag": tag.strip(), "sort": sort}

    def url(**changes) -> str:
        """This page with some settings changed; defaults are left out of the link."""
        params = settings | {"page": part.page} | changes
        defaults = {"view": "grid", "sort": "rating", "page": 1}
        return "/ratings?" + urlencode({k: v for k, v in params.items() if v not in ("", None) and defaults.get(k) != v})

    return templates.TemplateResponse(
        request,
        "ratings.html",
        {
            "rated": part.shown,
            "rated_total": len(rated),
            "total": part.total,
            "filtered": bool(rating or decade or tags.wanted),
            "page": part.page,
            "pages": part.pages,
            "here": current_url(request),
            "url": url,
            "decades": decades,
            "tag_options": tag_index.popular(),
            "unknown_tags": tag_index.unknown(tags.wanted),
            "sorts": RATINGS_SORTS,
            "poster_url": poster_url,
            "favorites": favorite_ids(session),
            **settings,
        },
    )


DIARY_PAGE_SIZE = 12
DIARY_TABS = ("playing", "played", "dropped", "favorites", "not-interested")


@app.get("/diary")
def diary_page(
    request: Request,
    show: str = "",
    q: str = "",
    tag: str = "",
    match: str = "all",
    page: str = "1",
    session: Session = Depends(get_session),
):
    """Your games, one status at a time, newest first, with your favorites and the games you're not
    interested in each in a tab."""
    rows = session.execute(
        select(DiaryEntry, Game)
        .join(Game, Game.steam_app_id == DiaryEntry.steam_app_id)
        .where(or_(DiaryEntry.status.is_not(None), DiaryEntry.favorite.is_(True), DiaryEntry.not_interested.is_(True)))
    ).all()

    def newest_first(pair):
        e = pair[0]
        return e.finished_on or e.started_on or date.min  # undated games last

    groups = {
        status: sorted(((e, g) for e, g in rows if e.status == status), key=newest_first, reverse=True)
        for status in diary.STATUSES
    }
    groups["favorites"] = sorted(((e, g) for e, g in rows if e.favorite), key=lambda p: p[1].name.lower())
    # Most recently changed first, so a game you just dismissed by mistake is on top.
    groups["not-interested"] = sorted(((e, g) for e, g in rows if e.not_interested), key=lambda p: p[0].updated_at, reverse=True)
    counts = {key: len(items) for key, items in groups.items()}
    if show not in groups:  # no tab chosen: the first diary list with something in it
        show = next((key for key in DIARY_TABS[:-1] if counts[key]), "playing")

    filters = ListFilter.read(q, tag, match)
    tag_index = TagIndex(g.tags for _, g in groups[show])
    part = paginate([(e, g) for e, g in groups[show] if filters.keeps(g)], page, DIARY_PAGE_SIZE)

    def url(**changes) -> str:
        """This page with some settings changed (a new tab starts on page 1)."""
        params = {
            "show": show,
            "q": q.strip(),
            "tag": tag.strip(),
            "match": "any" if (filters.wanted and filters.match == "any") else "",  # "all" is the default
            "page": part.page,
        } | changes
        return "/diary?" + urlencode({k: v for k, v in params.items() if v not in ("", None)})

    return templates.TemplateResponse(
        request,
        "diary.html",
        {
            "tabs": DIARY_TABS,
            "counts": counts,
            "show": show,
            "items": part.shown,
            "total": part.total,
            "filtered": filters.active,
            **filters.context(tag_index),
            "page": part.page,
            "pages": part.pages,
            "url": url,
        },
    )


@app.get("/stats")
def stats_page(request: Request, session: Session = Depends(get_session)):
    stats = compute_stats(session)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "stats": stats,
            "top_hours": stats.top_hours[0][1] if stats.top_hours else 1,
            "most_rated": max(stats.rating_counts.values(), default=0) or 1,
            "most_finished": max((n for _, n in stats.finished_by_year), default=0) or 1,
        },
    )


@app.get("/taste")
def taste_page(request: Request, session: Session = Depends(get_session)):
    taste = taste_profile(session, diary.load_ratings(session), diary.load_not_interested(session))
    ranked = sorted(taste.profile.items(), key=lambda kv: kv[1], reverse=True)
    toward = [(t, v) for t, v in ranked[:20] if v > 0]
    biggest = max([v for _, v in toward] + [1e-9])
    return templates.TemplateResponse(
        request,
        "taste.html",
        {
            "taste": taste,
            "toward": toward,
            "biggest": biggest,
            "taste_mode": get_taste_mode(session),
            "modes": TASTE_MODE_LABELS,
            "hide_adult": get_hide_adult(session),
        },
    )


@app.post("/taste/hide-adult")
def change_hide_adult(hide: str = Form("0"), session: Session = Depends(get_session)):
    """Leave games for adults out of recommendations and Explore, or show them."""
    set_hide_adult(session, hide == "1")
    return RedirectResponse("/taste#content", status_code=303)


@app.post("/taste/mode")
def change_taste_mode(mode: str = Form(""), session: Session = Depends(get_session)):
    """What teaches your taste: ratings and play, ratings only, or play only."""
    if mode in TASTE_MODES:
        set_taste_mode(session, mode)
    return RedirectResponse("/taste#learn", status_code=303)


# ---- actions ----------------------------------------------------------------


@app.post("/game/{app_id}/rate")
async def rate(
    request: Request,
    app_id: int,
    session: Session = Depends(get_session),
):
    # Read the raw form: FastAPI turns an empty field into "missing", but here "" means
    # "clear it" and a missing field means "leave it alone" (the stars and the review are
    # separate forms on some pages).
    form = await request.form()
    review = str(form["review"]) if "review" in form else None
    try:
        if "rating" in form:
            raw = str(form["rating"]).strip()
            rating = float(raw) if raw else None
        else:
            rating = diary.KEEP
        diary.save_rating(session, app_id, rating, review)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(
        safe_next(str(form.get("next") or ""), f"/game/{app_id}"), status_code=303
    )


@app.post("/game/{app_id}/hours")
def save_hours(
    app_id: int,
    hours: str = Form(""),
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        raw = hours.strip().replace(",", ".")
        set_hours(session, app_id, float(raw) if raw else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(safe_next(next, f"/game/{app_id}"), status_code=303)


@app.post("/game/{app_id}/not-interested")
def not_interested(
    app_id: int,
    value: str = Form("1"),
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        diary.set_not_interested(session, app_id, value == "1")
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return RedirectResponse(safe_next(next, f"/game/{app_id}"), status_code=303)


def parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, f"Not a date (use YYYY-MM-DD): {raw!r}")


@app.post("/game/{app_id}/status")
async def save_status(
    request: Request,
    app_id: int,
    session: Session = Depends(get_session),
):
    # Dates only change when the form has them: the quick status picker in the
    # library sends just the status and must not wipe the dates.
    form = await request.form()
    status = str(form.get("status") or "").strip() or None
    started = parse_date(str(form["started_on"])) if "started_on" in form else diary.KEEP
    finished = parse_date(str(form["finished_on"])) if "finished_on" in form else diary.KEEP
    try:
        diary.set_status(session, app_id, status, started, finished)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(
        safe_next(str(form.get("next") or ""), f"/game/{app_id}"), status_code=303
    )


@app.post("/game/{app_id}/favorite")
def favorite(
    app_id: int,
    value: str = Form("1"),
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        diary.set_favorite(session, app_id, value == "1")
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return RedirectResponse(safe_next(next, f"/game/{app_id}"), status_code=303)
