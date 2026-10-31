"""Steam store details (per-app) and SteamSpy community tags."""

import json
import re
from dataclasses import dataclass, field

import requests

from shelflife.throttle import RateLimited, Throttle

STORE_URL = "https://store.steampowered.com/api/appdetails"
STEAMSPY_URL = "https://steamspy.com/api.php"
HEADERS = {"User-Agent": "ShelfLife/0.1 (personal game diary project)"}

# The store allows roughly 200 requests / 5 min; SteamSpy asks for 1 request/second.
store_throttle = Throttle(1.6)
steamspy_throttle = Throttle(1.1)


@dataclass(frozen=True)
class StoreDetails:
    type: str | None
    short_description: str | None
    genres: list[str] = field(default_factory=list)
    release_date: str | None = None
    cover_url: str | None = None
    name: str | None = None


def fetch_store_details(app_id: int, timeout: float = 15) -> StoreDetails | None:
    """Return store info, or None when the store has no page for this app."""
    store_throttle.wait()
    resp = requests.get(
        STORE_URL,
        params={"appids": app_id, "l": "english"},
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
    )


def fetch_store_page_tags(app_id: int, timeout: float = 15) -> dict[str, int]:
    """Fallback for apps SteamSpy has no tags for: read the store page's tag votes."""
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


@dataclass(frozen=True)
class SpyListing:
    app_id: int
    name: str
    positive: int
    negative: int


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
# same fixed list of 70 games instead of an error.
UNKNOWN_TAG_LIST_SIZE = 70
SPY_TAG_ALIASES = {"roguelike": "rogue-like"}  # Steam's current name -> the one SteamSpy lists
_spy_spellings: dict[str, str] = {}  # the spelling that worked for a tag, remembered for the session


def spy_tag_spellings(tag: str) -> list[str]:
    """The spellings to try for a tag, most likely first, without repeats."""
    candidates = [tag, tag.lower(), tag.title(), SPY_TAG_ALIASES.get(tag.lower())]
    return [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]


def fetch_spy_tag_games(tag: str, timeout: float = 90) -> list[SpyListing]:
    """The games SteamSpy lists under a tag, trying the spellings it might know it by.
    Empty when none of them is a tag it recognises."""
    spellings = [_spy_spellings[tag]] if tag in _spy_spellings else spy_tag_spellings(tag)
    for spelling in spellings:
        listings = fetch_spy_tag_list(spelling, timeout)
        if listings and len(listings) != UNKNOWN_TAG_LIST_SIZE:
            _spy_spellings[tag] = spelling
            return listings
    return []


def fetch_spy_tag_list(tag: str, timeout: float = 90) -> list[SpyListing]:
    """Games SteamSpy lists under a tag, with review counts (can be thousands)."""
    data = _spy_get({"request": "tag", "tag": tag}, timeout)
    return [
        SpyListing(
            app_id=g["appid"],
            name=g.get("name", f"App {g['appid']}"),
            positive=g.get("positive", 0),
            negative=g.get("negative", 0),
        )
        for g in data.values()
    ]
