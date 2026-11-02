"""Steam store details (per-app), Steam's community tags, and SteamSpy's as a fallback."""

import html
import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

from shelflife.languages import parse_languages
from shelflife.throttle import RateLimited, Throttle

STORE_URL = "https://store.steampowered.com/api/appdetails"
STEAMSPY_URL = "https://steamspy.com/api.php"
STEAM_API_URL = "https://api.steampowered.com"
HEADERS = {"User-Agent": "ShelfLife/0.1 (personal game diary project)"}

# The store allows roughly 200 requests / 5 min; SteamSpy asks for 1 request/second.
store_throttle = Throttle(1.6)
steamspy_throttle = Throttle(1.1)
steamspy_all_throttle = Throttle(61)  # SteamSpy asks for one request a minute for its full list
steam_api_throttle = Throttle(1.0)  # Steam's tag calls: no published limit, so one a second


@dataclass(frozen=True)
class StoreDetails:
    type: str | None
    short_description: str | None
    genres: list[str] = field(default_factory=list)
    release_date: str | None = None
    cover_url: str | None = None
    name: str | None = None
    about_text: str = ""  # "About this game", as plain text
    languages: dict[str, str] = field(default_factory=dict)  # {"English": "audio", ...}
    price: dict = field(default_factory=dict)  # parse_price: {} when the store shows none
    info: dict = field(default_factory=dict)  # parse_info: developers, categories, platforms...


_MARKUP = re.compile(r"<[^>]*>")
_LINK = re.compile(r"https?://\S+")


def clean_store_text(raw: str | None) -> str:
    """The store's HTML description as plain text: no tags, links or extra white space."""
    text = html.unescape(_MARKUP.sub(" ", raw or ""))
    return " ".join(_LINK.sub(" ", text).split())


def _store_params(**params) -> dict:
    """The store call's parameters. The store answers in the currency of where the request seems
    to come from (a VPN changes it); STEAM_COUNTRY in .env (e.g. "BR") asks for that country's."""
    params = {"l": "english", **params}
    if country := os.getenv("STEAM_COUNTRY", "").strip():
        params["cc"] = country.lower()
    return params


PRICE_BATCH = 100  # the store gives this many games' prices in one call


def fetch_prices(app_ids: list[int], timeout: float = 30) -> dict[int, dict]:
    """Current prices (parse_price) of many games, PRICE_BATCH per store call. A game the store
    doesn't know is left out; one it knows but sells no more, or that is free, gets {} (asked for
    the price alone, the store doesn't say "free": callers keep a "free" they already know)."""
    out: dict[int, dict] = {}
    for start in range(0, len(app_ids), PRICE_BATCH):
        chunk = app_ids[start : start + PRICE_BATCH]
        store_throttle.wait()
        resp = requests.get(
            STORE_URL,
            params=_store_params(appids=",".join(map(str, chunk)), filters="price_overview"),
            headers=HEADERS,
            timeout=timeout,
        )
        if resp.status_code in (403, 429):
            raise RateLimited("Steam store is rate limiting us; try again in a few minutes.")
        resp.raise_for_status()
        answers = resp.json() or {}
        for app_id in chunk:
            entry = answers.get(str(app_id)) or {}
            if not entry.get("success"):
                continue
            data = entry.get("data")
            out[app_id] = parse_price(data if isinstance(data, dict) else {})
    return out


def fetch_store_details(app_id: int, timeout: float = 15) -> StoreDetails | None:
    """Return store info, or None when the store has no page for this app."""
    store_throttle.wait()
    resp = requests.get(
        STORE_URL,
        params=_store_params(appids=app_id),
        headers=HEADERS,
        timeout=timeout,
    )
    if resp.status_code in (403, 429):
        raise RateLimited("Steam store is rate limiting us; try again in a few minutes.")
    resp.raise_for_status()

    entry = (resp.json() or {}).get(str(app_id), {})
    if not entry.get("success"):
        return None
    data = entry["data"]
    return StoreDetails(
        type=data.get("type"),
        short_description=data.get("short_description"),
        genres=[g["description"] for g in data.get("genres", [])],
        release_date=(data.get("release_date") or {}).get("date") or None,
        cover_url=data.get("header_image"),
        name=data.get("name"),
        about_text=clean_store_text(data.get("about_the_game")),
        languages=parse_languages(data.get("supported_languages")),
        price=parse_price(data),
        info=parse_info(data),
    )


SCREENSHOTS_KEPT = 6


def parse_info(data: dict) -> dict:
    """The store details ShelfLife keeps for later without using them yet: who made the game,
    Steam's own categories (single-player, co-op, PvP...), platforms, Metacritic, age and mature
    content flags, whether it's out yet, counts, and a few pictures. Missing ones are left out."""
    info: dict = {
        "developers": [x for x in data.get("developers") or [] if x],
        "publishers": [x for x in data.get("publishers") or [] if x],
        "categories": [c["description"] for c in data.get("categories") or [] if c.get("description")],
        "platforms": [p for p, ok in (data.get("platforms") or {}).items() if ok],
        "coming_soon": bool((data.get("release_date") or {}).get("coming_soon")),
        "dlc_count": len(data.get("dlc") or []),
        "screenshots": [s["path_full"] for s in (data.get("screenshots") or [])[:SCREENSHOTS_KEPT] if s.get("path_full")],
    }
    if data.get("controller_support"):
        info["controller_support"] = data["controller_support"]
    if (score := (data.get("metacritic") or {}).get("score")) is not None:
        info["metacritic"] = score
    try:
        if age := int(data.get("required_age") or 0):
            info["required_age"] = age
    except (TypeError, ValueError):
        pass
    if ids := (data.get("content_descriptors") or {}).get("ids"):
        info["content_descriptors"] = ids  # 1 some nudity or sexual content, 2 violence or gore,
        # 3 adult only sexual content, 4 frequent nudity or sexual content, 5 general mature content
    if (total := (data.get("achievements") or {}).get("total")) is not None:
        info["achievements"] = total
    if (total := (data.get("recommendations") or {}).get("total")) is not None:
        info["recommendations"] = total
    for key, name in (("website", "website"), ("background_raw", "background")):
        if data.get(key):
            info[name] = data[key]
    return info


def parse_price(data: dict) -> dict:
    """The price the store shows, in the currency of the country the request comes from:
    {"free": True} for a free game; {"currency": "BRL", "initial": 5999, "final": 2999, "discount": 50}
    (in cents) otherwise; {} when there's none (not released yet, or not sold any more)."""
    if data.get("is_free"):
        return {"free": True}
    overview = data.get("price_overview") or {}
    if not overview.get("currency") or overview.get("final") is None:
        return {}
    return {
        "currency": overview["currency"],
        "initial": overview.get("initial", overview["final"]),
        "final": overview["final"],
        "discount": overview.get("discount_percent", 0),
    }


@dataclass(frozen=True)
class Listing:
    """A game in a list of games (under a tag, or by popularity) with its review counts, and its
    tags when the list brings them (Steam's does, SteamSpy's doesn't)."""

    app_id: int
    name: str
    positive: int
    negative: int
    tags: dict[str, int] | None = field(default=None, compare=False, hash=False)


# ---- Steam's own tags ----
# Steam's API names its tags (IStoreService/GetTagList), gives games' tags (IStoreBrowseService/
# GetItems, TAGS_BATCH games a call) and lists the games under a tag (IStoreQueryService/Query).
# No key is needed. Valve's public docs don't describe the last two (the store itself uses them),
# so SteamSpy stays as the fallback. A game's tag weights aren't raw votes (about a sixth of
# SteamSpy's counts) but come in the same order, and tags.tag_weights only uses each tag's share
# of the top one.
TAGS_BATCH = 200  # games per GetItems call (500 is refused)
TAGS_PER_GAME = 20
TAG_NAMES_HOURS = 24
# The tag query's order. 30 is Steam's popularity order (undocumented; measured on 2026-11-02, the
# closest to "most reviewed first"); pages are read until one brings fewer than TAG_QUERY_ENOUGH
# games with enough reviews, at most TAG_QUERY_PAGES of TAG_QUERY_PAGE (about 2 seconds each).
TAG_QUERY_SORT = 30
TAG_QUERY_PAGE = 1000
TAG_QUERY_PAGES = 5
TAG_QUERY_ENOUGH = 20
# SteamSpy's old names -> Steam's (other differences are only letter case; the rest Steam retired)
SPY_RENAMED = {
    "rogue-like": "Roguelike",
    "rogue-lite": "Roguelite",
    "puzzle-platformer": "Puzzle Platformer",
    "base-building": "Base Building",
    "e-sports": "eSports",
}
_tag_names: tuple[float, dict[int, str]] | None = None


def _country() -> str:
    """The country Steam's tag calls ask as (they need one): STEAM_COUNTRY, or the US."""
    return (os.getenv("STEAM_COUNTRY", "").strip() or "US").upper()


def _steam_get(method: str, params: dict, timeout: float) -> dict:
    steam_api_throttle.wait()
    resp = requests.get(f"{STEAM_API_URL}/{method}/v1/", params=params, headers=HEADERS, timeout=timeout)
    if resp.status_code in (403, 429):
        raise RateLimited("Steam is rate limiting us; try again in a few minutes.")
    resp.raise_for_status()
    return (resp.json() or {}).get("response") or {}


def _steam_query(method: str, body: dict, timeout: float) -> dict:
    return _steam_get(method, {"input_json": json.dumps(body)}, timeout)


def steam_tag_names(timeout: float = 30) -> dict[int, str]:
    """Steam's tags, id -> English name; read once a day."""
    global _tag_names
    if _tag_names is not None and time.monotonic() - _tag_names[0] < TAG_NAMES_HOURS * 3600:
        return _tag_names[1]
    answer = _steam_get("IStoreService/GetTagList", {"language": "english"}, timeout)
    names = {t["tagid"]: t["name"] for t in answer.get("tags") or [] if t.get("tagid") and t.get("name")}
    if names:
        _tag_names = (time.monotonic(), names)
    return names


def steam_tag_id(name: str) -> int | None:
    """Steam's id for a tag name (any letter case; SteamSpy's old names too), or None."""
    wanted = SPY_RENAMED.get(name.strip().lower(), name.strip()).lower()
    return next((i for i, n in steam_tag_names().items() if n.lower() == wanted), None)


def steam_names(tags: dict[str, int]) -> dict[str, int]:
    """Tags named as Steam names them now: SteamSpy's old names renamed, Steam's letter case, and
    tags Steam has retired ("Mature", "NSFW", "Drama"...) dropped. When Steam's list can't be read,
    only the renaming is done."""
    try:
        known = {n.lower(): n for n in steam_tag_names().values()}
    except (RateLimited, requests.RequestException, ValueError):
        known = {}
    out: dict[str, int] = {}
    for name, votes in tags.items():
        name = SPY_RENAMED.get(name.lower(), name)
        if known:
            name = known.get(name.lower())
            if name is None:
                continue
        out[name] = max(votes, out.get(name, 0))
    return out


def _item_tags(item: dict, names: dict[int, str]) -> dict[str, int]:
    return {names[t["tagid"]]: t.get("weight", 0) for t in item.get("tags") or [] if t.get("tagid") in names}


def fetch_steam_tags(app_ids: list[int], timeout: float = 60) -> dict[int, dict[str, int]]:
    """Games' tags from Steam, TAGS_BATCH games a call: app id -> {name: weight}. Games Steam has
    no tags for, or doesn't sell in your country, are left out."""
    names = steam_tag_names()
    wanted = set(app_ids)
    out: dict[int, dict[str, int]] = {}
    for start in range(0, len(app_ids), TAGS_BATCH):
        body = {
            "ids": [{"appid": a} for a in app_ids[start : start + TAGS_BATCH]],
            "context": {"language": "english", "country_code": _country()},
            "data_request": {"include_tag_count": TAGS_PER_GAME},
        }
        for item in _steam_query("IStoreBrowseService/GetItems", body, timeout).get("store_items") or []:
            app_id = item.get("appid")
            # A game Steam has moved answers under its new id: that one wasn't asked for.
            if app_id in wanted and item.get("success") == 1 and (tags := _item_tags(item, names)):
                out[app_id] = tags
    return out


def _steam_listing(item: dict, names: dict[int, str]) -> Listing:
    summary = (item.get("reviews") or {}).get("summary_filtered") or {}
    count = summary.get("review_count") or 0
    positive = round(count * (summary.get("percent_positive") or 0) / 100)
    app_id = item["appid"]
    return Listing(app_id, item.get("name") or f"App {app_id}", positive, count - positive, _item_tags(item, names))


def fetch_steam_tag_games(tag_id: int, min_reviews: int = 0, timeout: float = 90) -> list[Listing]:
    """The released games Steam lists under a tag, with their review counts and tags, in Steam's
    popularity order. Reads pages until one brings few games with `min_reviews` (see
    TAG_QUERY_ENOUGH), so a huge tag like Indie isn't read to the end."""
    names = steam_tag_names()
    out: list[Listing] = []
    for page in range(TAG_QUERY_PAGES):
        body = {
            "query": {
                "start": page * TAG_QUERY_PAGE,
                "count": TAG_QUERY_PAGE,
                "sort": TAG_QUERY_SORT,
                "filters": {
                    "released_only": True,
                    "type_filters": {"include_games": True},
                    "tagids_must_match": [{"tagids": [tag_id]}],
                },
            },
            "context": {"language": "english", "country_code": _country()},
            "data_request": {"include_basic_info": True, "include_reviews": True, "include_tag_count": TAGS_PER_GAME},
        }
        answer = _steam_query("IStoreQueryService/Query", body, timeout)
        listings = [_steam_listing(item, names) for item in answer.get("store_items") or [] if item.get("appid")]
        out.extend(listings)
        total = (answer.get("metadata") or {}).get("total_matching_records") or 0
        enough = sum(g.positive + g.negative >= min_reviews for g in listings)
        if len(listings) < TAG_QUERY_PAGE or len(out) >= total or enough < TAG_QUERY_ENOUGH:
            break
    return out


def fetch_store_page_tags(app_id: int, timeout: float = 15) -> dict[str, int]:
    """Last resort for apps neither Steam's API nor SteamSpy has tags for: read the store page's tag votes."""
    store_throttle.wait()
    resp = requests.get(
        f"https://store.steampowered.com/app/{app_id}/",
        params={"l": "english"},
        # Skips the age-gate interstitial so mature games still return their page.
        cookies={"birthtime": "0", "lastagecheckage": "1-0-1990", "wants_mature_content": "1"},
        headers=HEADERS,
        timeout=timeout,
    )
    if resp.status_code in (403, 429):
        raise RateLimited("Steam store is rate limiting us; try again in a few minutes.")
    resp.raise_for_status()

    match = re.search(r"InitAppTagModal\(\s*\d+,\s*(\[.*?\])\s*,", resp.text)
    if not match:
        return {}
    return {t["name"]: t["count"] for t in json.loads(match.group(1))}


@dataclass(frozen=True)
class SpyDetails:
    tags: dict[str, int]
    positive: int | None
    negative: int | None


def _spy_get(params: dict, timeout: float) -> dict:
    steamspy_throttle.wait()
    resp = requests.get(STEAMSPY_URL, params=params, headers=HEADERS, timeout=timeout)
    if resp.status_code in (403, 429):
        raise RateLimited("SteamSpy is rate limiting us; try again in a few minutes.")
    resp.raise_for_status()
    return resp.json()


def fetch_spy_details(app_id: int, timeout: float = 15) -> SpyDetails:
    """Community tags -> votes plus review counts from SteamSpy."""
    data = _spy_get({"request": "appdetails", "appid": app_id}, timeout)
    tags = data.get("tags")
    # SteamSpy returns [] instead of {} when an app has no tags.
    return SpyDetails(
        tags=tags if isinstance(tags, dict) else {},
        positive=data.get("positive"),
        negative=data.get("negative"),
    )


def fetch_tags(app_id: int, timeout: float = 15) -> dict[str, int]:
    """Return community tags -> votes from SteamSpy ({} if it has none)."""
    return fetch_spy_details(app_id, timeout).tags


# SteamSpy's tag lists answer to a tag's name only in the letter case it keeps it under
# ("metroidvania" works, "Metroidvania" returns nothing; "Souls-Like" works, "Souls-like"
# does not), a few tags only under their old name, and a tag it does not know gets the
# same fixed list of 70 games instead of an error. Tags with an apostrophe ("Beat 'em up",
# "Shoot 'Em Up") are not known to it under any spelling.
UNKNOWN_TAG_LIST_SIZE = 70
SPY_TAG_ALIASES = {"roguelike": "rogue-like", "roguelite": "rogue-lite"}  # Steam's current name -> the one SteamSpy lists
_spy_spellings: dict[str, str] = {}  # the spelling that worked for a tag, remembered for the session


def spy_tag_spellings(tag: str) -> list[str]:
    """The spellings to try for a tag, most likely first, without repeats."""
    candidates = [tag, tag.lower(), tag.title(), SPY_TAG_ALIASES.get(tag.lower())]
    return [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]


def fetch_spy_tag_games(tag: str, timeout: float = 90) -> list[Listing]:
    """The games SteamSpy lists under a tag, trying the spellings it might know it by.
    Empty when none of them is a tag it recognises."""
    spellings = [_spy_spellings[tag]] if tag in _spy_spellings else spy_tag_spellings(tag)
    for spelling in spellings:
        listings = fetch_spy_tag_list(spelling, timeout)
        if listings and len(listings) != UNKNOWN_TAG_LIST_SIZE:
            _spy_spellings[tag] = spelling
            return listings
    return []


def fetch_spy_tag_list(tag: str, timeout: float = 90) -> list[Listing]:
    """Games SteamSpy lists under a tag, with review counts (can be thousands)."""
    return _listings(_spy_get({"request": "tag", "tag": tag}, timeout))


def _listings(data: dict) -> list[Listing]:
    return [
        Listing(
            app_id=g["appid"],
            name=g.get("name", f"App {g['appid']}"),
            positive=g.get("positive", 0),
            negative=g.get("negative", 0),
        )
        for g in data.values()
    ]


# SteamSpy's full list comes most popular first, 1,000 games a page (page 0: the top
# 1,000). Its "top 100" lists all return the same games now, so they are not used.
PAGE_CACHE_SECONDS = 6 * 3600
_pages: dict[int, tuple[float, list[Listing]]] = {}


def fetch_spy_page(page: int, timeout: float = 120) -> list[Listing]:
    """One page of SteamSpy's full list of games with their review counts, most popular
    first. Waits as SteamSpy asks (one such request a minute) and remembers a page for a
    few hours, so a second search does not wait again."""
    cached = _pages.get(page)
    if cached is not None and time.monotonic() - cached[0] < PAGE_CACHE_SECONDS:
        return cached[1]
    steamspy_all_throttle.wait()
    resp = requests.get(
        STEAMSPY_URL, params={"request": "all", "page": page}, headers=HEADERS, timeout=timeout
    )
    if resp.status_code in (403, 429):
        raise RateLimited("SteamSpy is rate limiting us; try again in a few minutes.")
    resp.raise_for_status()
    listings = _listings(resp.json())
    _pages[page] = (time.monotonic(), listings)
    return listings
