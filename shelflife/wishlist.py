"""Your Steam wishlist: read it, keep it in step with Steam, and sort it.

Valve's `IWishlistService/GetWishlist` returns only a game id, your own rank for it and the date
you added it. It needs no key, only your SteamID, and a private wishlist comes back empty. The
endpoint is not part of Valve's documented API (see "Privacy" in the README).

Wishlisting is wanting, not liking, so it never teaches your taste profile.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.models import Game, WishlistItem
from shelflife.store import HEADERS
from shelflife.throttle import RateLimited

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"


@dataclass(frozen=True)
class WishlistEntry:
    app_id: int
    priority: int | None  # your own rank, 1 at the top; 0 or None: you never ranked it
    added_at: datetime | None


@dataclass(frozen=True)
class WishlistImport:
    total: int  # games on the wishlist afterwards
    added: int  # new on it since the last import
    removed: int  # gone from it (bought, or removed by you)
    kept_old_list: bool = False  # Steam answered with an empty list, so the earlier one was kept


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fetch_wishlist(steam_id: str, timeout: float = 30) -> list[WishlistEntry]:
    """Your wishlist as Steam has it. Empty if it is empty or private."""
    resp = requests.get(WISHLIST_URL, params={"steamid": steam_id}, headers=HEADERS, timeout=timeout)
    if resp.status_code in (403, 429):
        raise RateLimited("Steam is rate limiting us; try again in a few minutes.")
    resp.raise_for_status()
    items = (resp.json().get("response") or {}).get("items") or []
    return [
        WishlistEntry(
            app_id=int(i["appid"]),
            priority=i.get("priority"),
            added_at=(
                datetime.fromtimestamp(i["date_added"], timezone.utc).replace(tzinfo=None) if i.get("date_added") else None
            ),
        )
        for i in items
        if "appid" in i
    ]


def import_wishlist(session: Session, entries: list[WishlistEntry]) -> WishlistImport:
    """Make the saved wishlist match `entries`: new games are added (as bare Game rows when
    unknown, to have their details fetched later), games no longer listed are removed.

    An empty list from Steam when you had one before is treated as "private or failed", not
    "you removed everything": the earlier wishlist is kept.
    """
    now = _now()
    existing = {w.steam_app_id: w for w in session.scalars(select(WishlistItem))}
    if not entries and existing:
        return WishlistImport(total=len(existing), added=0, removed=0, kept_old_list=True)

    seen: set[int] = set()
    added = 0
    for entry in entries:
        seen.add(entry.app_id)
        if session.get(Game, entry.app_id) is None:
            session.add(Game(steam_app_id=entry.app_id, name=f"App {entry.app_id}"))  # named when its details arrive
        item = existing.get(entry.app_id)
        if item is None:
            session.add(WishlistItem(steam_app_id=entry.app_id, priority=entry.priority, added_at=entry.added_at, seen_at=now))
            added += 1
        else:
            item.priority, item.added_at, item.seen_at = entry.priority, entry.added_at, now
    gone = [item for app_id, item in existing.items() if app_id not in seen]
    for item in gone:
        session.delete(item)
    session.commit()
    return WishlistImport(total=len(seen), added=added, removed=len(gone))


def rank_label(priority: int | None) -> str | None:
    """"#12" for a game you ranked, None for one you did not."""
    return f"#{priority}" if priority else None


def order_key(item: WishlistItem) -> tuple:
    """Your own order: ranked games by rank, then the unranked ones, newest first."""
    ranked = bool(item.priority)
    added = item.added_at.timestamp() if item.added_at else 0
    return (0, item.priority, 0) if ranked else (1, 0, -added)
