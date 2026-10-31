"""Local web app: recommendations, your library, exploring Steam, game pages and your diary.

Runs on your own machine only (see scripts/web.py). There is no login: anyone who
can reach the port can change your diary, so it binds to 127.0.0.1.
"""

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from shelflife import diary
from shelflife.db import SessionLocal, init_db
from shelflife.hours import set_hours
from shelflife.models import DiaryEntry, Game, LibraryEntry
from shelflife.quality import quality_factor
from shelflife.recency import recency
from shelflife.discover_job import SECONDS_PER_GAME, job as discover_job
from shelflife.discovery import MIN_REVIEWS
from shelflife.recommend import explain, recommend, score_all, taste_profile, top_taste_tags
from shelflife.score import engagement_score
from shelflife.settings import TASTE_MODE_HELP, TASTE_MODES, get_taste_mode, set_taste_mode
from shelflife.similar import search_tags, similar_to, tag_idf
from shelflife.stats import compute_stats
from shelflife.tagfilter import TagIndex, matches, parse_match, parse_tags

BASE = Path(__file__).parent
PAGE_SIZE = 40
RATING_OPTIONS = [r / 2 for r in range(1, 11)]  # 0.5 ... 5.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ShelfLife", lifespan=lifespan)
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


templates.env.globals.update(
    RATING_OPTIONS=RATING_OPTIONS,
    STATUSES=diary.STATUSES,
    STATUS_LABELS=diary.STATUS_LABELS,
    reviews_text=reviews_text,
    rating_text=rating_text,
    source_label=source_label,
)


def safe_next(url: str | None, default: str) -> str:
    """Only redirect to paths on this site."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return default


# ---- pages ------------------------------------------------------------------


REC_PAGE_SIZES = (6, 12, 24, 48)
REC_PAGE_PARAMS = {"backlog": "bp", "family": "fp", "discover": "dp"}


@app.get("/")
def home(
    request: Request,
    size: int = 12,
    bp: int = 1,
    fp: int = 1,
    dp: int = 1,
    session: Session = Depends(get_session),
):
    size = size if size in REC_PAGE_SIZES else 12
    ratings = diary.load_ratings(session)
    result = recommend(
        session,
        ratings,
        diary.load_not_interested(session),
        limit=100_000,  # everything; the page shows `size` at a time
    )

    wanted = {"backlog": bp, "family": fp, "discover": dp}
    sections = {}
    for key, recs in (
        ("backlog", result.backlog),
        ("family", result.family),
        ("discover", result.discover),
    ):
        pages = max(1, -(-len(recs) // size))
        page = max(1, min(wanted[key], pages))
        sections[key] = {
            "recs": recs[(page - 1) * size : page * size],  # not "items": clashes with dict.items in Jinja
            "total": len(recs),
            "page": page,
            "pages": pages,
        }

    def page_url(key: str, page: int | None = None) -> str:
        """Link to this page with one list moved to `page`, keeping the others."""
        params = {"size": size}
        for k, param in REC_PAGE_PARAMS.items():
            params[param] = sections[k]["page"]
        if page is not None:
            params[REC_PAGE_PARAMS[key]] = page
        return f"/?{urlencode(params)}#{key}"

    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {
            "taste": result.taste,
            "sections": sections,
            "page_url": page_url,
            "size": size,
            "sizes": REC_PAGE_SIZES,
            "ratings": ratings,
            "taste_mode": TASTE_MODES[get_taste_mode(session)],
        },
    )


REC_FILTERS = {"all": None, "50": 50, "25": 25, "10": 10}  # "recommended": top N% by fit
EXPLORE_PAGE_SIZE = 24


def top_share(scores: dict[int, float], percent: int | None) -> set[int] | None:
    """Ids in the top `percent`% by score; None means no filter."""
    if percent is None:
        return None
    ranked = sorted(scores, key=scores.get, reverse=True)
    keep = max(1, -(-len(ranked) * percent // 100)) if ranked else 0
    return set(ranked[:keep])


def current_url(request: Request) -> str:
    """This page's path and query, to come back to after an action."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


@app.get("/library")
def library(
    request: Request,
    q: str = "",
    source: str = "all",
    sort: str = "",
    rec: str = "all",
    status: str = "all",
    tag: str = "",
    match: str = "all",
    page: int = 1,
    session: Session = Depends(get_session),
):
    ratings = diary.load_ratings(session)
    entries = diary.load_entries(session)
    wanted = parse_tags(tag)
    match = parse_match(match)
    status = status if status in (*diary.STATUSES, "none") else "all"
    scores = score_all(session, ratings, diary.load_not_interested(session))
    scope = scores.backlog | scores.family  # what "recommended" means in the library
    keep = top_share({i: scores.by_id[i].score for i in scope}, REC_FILTERS.get(rec))
    sort = sort or ("fit" if keep is not None else "hours")

    query = (
        select(Game)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(or_(Game.type == "game", Game.type.is_(None)))
    )
    if source == "owned":
        query = query.where(Game.family_shared.is_not(True))
    elif source == "family":
        query = query.where(Game.family_shared.is_(True))

    needle = q.strip().lower()
    games = list(session.scalars(query))
    tag_index = TagIndex(g.tags for g in games)  # the whole source, before search and tag filters
    rows = []
    for game in games:
        if keep is not None and game.steam_app_id not in keep:
            continue
        if (needle and needle not in game.name.lower()) or not matches(game.tags, wanted, match):
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

    keys = {
        "name": lambda r: r["game"].name.lower(),
        "hours": lambda r: -(r["hours"] or 0),
        "score": lambda r: -(r["score"] or 0),
        "fit": lambda r: -(r["fit"] if r["fit"] is not None else -1e9),
        "rating": lambda r: -(r["rating"] or 0),
        # Most recent first; games with no date (family games, never played) last.
        "recent": lambda r: (r["last_played"] is None, -(r["last_played"].timestamp() if r["last_played"] else 0)),
    }
    rows.sort(key=keys.get(sort, keys["hours"]))

    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = max(1, min(page, pages))
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "rows": rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
            "total": len(rows),
            "q": q,
            "source": source,
            "sort": sort,
            "rec": rec if rec in REC_FILTERS else "all",
            "status": status,
            "tag": tag,
            "match": match,
            "tag_options": tag_index.popular(),
            "unknown_tags": tag_index.unknown(wanted),
            "page": page,
            "pages": pages,
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
    page: int = 1,
    session: Session = Depends(get_session),
):
    """Games you don't own: the discovery pool, ranked by how well they fit you, or by
    how like one game (`like`) they are."""
    wanted = parse_tags(tag)
    match = parse_match(match)
    ratings = diary.load_ratings(session)
    not_interested = diary.load_not_interested(session)
    scores = score_all(session, ratings, not_interested)
    keep = top_share({i: scores.by_id[i].score for i in scores.discover}, REC_FILTERS.get(rec))

    query = (
        select(Game)
        .outerjoin(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(LibraryEntry.steam_app_id.is_(None), Game.type == "game")
    )
    needle = q.strip().lower()
    games = list(session.scalars(query))
    tag_index = TagIndex(g.tags for g in games)
    recs = []
    for game in games:
        rec_item = scores.by_id.get(game.steam_app_id)  # None: no tags yet
        if (
            rec_item is None
            or game.steam_app_id in not_interested
            or (needle and needle not in game.name.lower())
            or (keep is not None and game.steam_app_id not in keep)
            or not matches(game.tags, wanted, match)
        ):
            continue
        recs.append(rec_item)

    ref = None  # the game "like this" is about
    if like.strip().isdigit():
        found = session.get(Game, int(like.strip()))
        ref = found if found is not None and found.tags else None
    like_tags = []
    if ref is not None:
        idf = tag_idf(session)
        recs = similar_to(ref, [r.game for r in recs], idf)  # same games, ranked by likeness to ref
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

    pages = max(1, -(-len(recs) // EXPLORE_PAGE_SIZE))
    page = max(1, min(page, pages))
    return templates.TemplateResponse(
        request,
        "explore.html",
        {
            "recs": recs[(page - 1) * EXPLORE_PAGE_SIZE : page * EXPLORE_PAGE_SIZE],
            "total": len(recs),
            "q": q,
            "sort": sort,
            "sorts": sorts,
            "ref": ref,
            "like_tags": like_tags,
            "rec": rec if rec in REC_FILTERS else "all",
            "tag": tag,
            "match": match,
            "tag_options": tag_index.popular(),
            "unknown_tags": tag_index.unknown(wanted),
            "page": page,
            "pages": pages,
            "here": current_url(request),
            "ratings": ratings,
            "job": discover_job.snapshot(),
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
    session: Session = Depends(get_session),
):
    """Look for new games to explore: starts a background search and returns at once.
    With `like` (a game's app id) it looks for close matches to that game instead."""
    if like.strip():
        ref = session.get(Game, int(like)) if like.strip().isdigit() else None
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

    wanted = parse_tags(tags)
    if wanted:
        index = TagIndex(session.scalars(select(Game.tags)))
        wanted = [index.spelling(t) for t in wanted][:MAX_FIND_TAGS]
    else:  # no tags typed: search for the tags you like most
        wanted = top_taste_tags(session, diary.load_ratings(session), diary.load_not_interested(session))
    if not wanted:
        discover_job.fail("Rate a few games first so there are taste tags to search for, or type some tags.")
    else:
        discover_job.start(
            wanted,
            choice(count, FIND_COUNTS, 25),
            choice(min_reviews, FIND_MIN_REVIEWS, MIN_REVIEWS),
        )
    return RedirectResponse("/explore#find", status_code=303)


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
        },
    )


@app.get("/ratings")
def ratings_page(request: Request, session: Session = Depends(get_session)):
    entries = session.execute(
        select(DiaryEntry, Game)
        .join(Game, Game.steam_app_id == DiaryEntry.steam_app_id)
        .order_by(DiaryEntry.rating.desc().nulls_last(), Game.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "ratings.html",
        {
            # A game can have just a review and no rating, so list those too.
            "rated": [(e, g) for e, g in entries if e.rating is not None or e.review],
            "marked": [(e, g) for e, g in entries if e.not_interested],
        },
    )


DIARY_PAGE_SIZE = 12
DIARY_TABS = ("playing", "played", "dropped", "favorites")


@app.get("/diary")
def diary_page(
    request: Request,
    show: str = "",
    q: str = "",
    tag: str = "",
    match: str = "all",
    page: int = 1,
    session: Session = Depends(get_session),
):
    """Your games, one status at a time, newest first, with your favorites in a tab."""
    rows = session.execute(
        select(DiaryEntry, Game)
        .join(Game, Game.steam_app_id == DiaryEntry.steam_app_id)
        .where(or_(DiaryEntry.status.is_not(None), DiaryEntry.favorite.is_(True)))
    ).all()

    def newest_first(pair):
        e = pair[0]
        return e.finished_on or e.started_on or date.min  # undated games last

    groups = {
        status: sorted(((e, g) for e, g in rows if e.status == status), key=newest_first, reverse=True)
        for status in diary.STATUSES
    }
    groups["favorites"] = sorted(((e, g) for e, g in rows if e.favorite), key=lambda p: p[1].name.lower())
    counts = {key: len(items) for key, items in groups.items()}
    if show not in groups:  # no tab chosen: the first one with something in it
        show = next((key for key in DIARY_TABS if counts[key]), "playing")

    wanted = parse_tags(tag)
    match = parse_match(match)
    needle = q.strip().lower()
    tag_index = TagIndex(g.tags for _, g in groups[show])
    items = [
        (e, g)
        for e, g in groups[show]
        if (not needle or needle in g.name.lower()) and matches(g.tags, wanted, match)
    ]
    pages = max(1, -(-len(items) // DIARY_PAGE_SIZE))
    page = max(1, min(page, pages))

    def url(**changes) -> str:
        """This page with some settings changed (a new tab starts on page 1)."""
        params = {
            "show": show,
            "q": q.strip(),
            "tag": tag.strip(),
            "match": "any" if (wanted and match == "any") else "",  # "all" is the default
            "page": page,
        } | changes
        return "/diary?" + urlencode({k: v for k, v in params.items() if v not in ("", None)})

    return templates.TemplateResponse(
        request,
        "diary.html",
        {
            "tabs": DIARY_TABS,
            "counts": counts,
            "show": show,
            "items": items[(page - 1) * DIARY_PAGE_SIZE : page * DIARY_PAGE_SIZE],
            "total": len(items),
            "filtered": bool(needle or wanted),
            "q": q,
            "tag": tag,
            "match": match,
            "tag_options": tag_index.popular(),
            "unknown_tags": tag_index.unknown(wanted),
            "page": page,
            "pages": pages,
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
    away = [(t, v) for t, v in ranked[::-1][:12] if v < 0]
    biggest = max([v for _, v in toward] + [-v for _, v in away] + [1e-9])
    return templates.TemplateResponse(
        request,
        "taste.html",
        {
            "taste": taste,
            "toward": toward,
            "away": away,
            "biggest": biggest,
            "mode": get_taste_mode(session),
            "modes": TASTE_MODES,
            "mode_help": TASTE_MODE_HELP,
        },
    )


@app.post("/taste/mode")
def change_taste_mode(mode: str = Form(""), session: Session = Depends(get_session)):
    """Choose what your taste learns from: ratings, how you play, or both."""
    try:
        set_taste_mode(session, mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse("/taste#mode", status_code=303)


# ---- actions ----------------------------------------------------------------


@app.post("/game/{app_id}/rate")
async def rate(
    request: Request,
    app_id: int,
    session: Session = Depends(get_session),
):
    # Read the raw form: FastAPI turns an empty field into "missing", but here
    # "" means "clear the review" and a missing field means "leave it alone".
    form = await request.form()
    rating = str(form.get("rating") or "").strip()
    review = str(form["review"]) if "review" in form else None
    try:
        diary.save_rating(session, app_id, float(rating) if rating else None, review)
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
