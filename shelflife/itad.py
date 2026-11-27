"""A game's lowest price ever on Steam, from IsThereAnyDeal (https://isthereanydeal.com).

Steam's store only says what a game costs now, so ShelfLife can't know whether a sale is a good
one. IsThereAnyDeal records every store's prices over the years; its API gives, per game and
country, the lowest price each store ever had, with its date. ShelfLife asks only for Steam's
low (shop 61) for the games you don't own, in your STEAM_COUNTRY, and shows it beside the price,
with a "lowest ever" flag when today's price is that low.

Two calls per 200 games: one turns Steam app ids into IsThereAnyDeal's own game ids (asked once
per game, then kept), one reads their Steam lows. Sync reads your wishlist's every day and the
rest every week, like prices (shelflife/sync.py, lows_due).

Terms (read 2026-11-26, from github.com/IsThereAnyDeal/API: docs/docs.yaml and
TERMS_OF_SERVICE.md): use is allowed for an app "available to public" (ShelfLife is public and
open source, and each user brings their own key); credit IsThereAnyDeal (the pages link it); the
data must not be changed (shown as given, only formatted); no app that competes with
IsThereAnyDeal (this shows Steam's own low, nothing about other stores); 100 requests per 5
minutes for a new key. Needs ITAD_API_KEY in .env (register an app at isthereanydeal.com/apps/my/).
What is sent: Steam app ids, IsThereAnyDeal game ids and your STEAM_COUNTRY.
"""

import os
from datetime import datetime, timedelta

import requests

import shelflife.config  # noqa: F401  (reads .env, where the key is)
from shelflife.throttle import RateLimited, Throttle

API_URL = "https://api.isthereanydeal.com"
STEAM_SHOP = 61
BATCH = 200  # the most game ids one call takes
# A new key allows 100 requests per 5 minutes (the owner's, 2026-11-26; the docs say 1000 for verified
# accounts): one every 3.1 seconds stays under it. A first read of 3,400 games is about 34 requests.
itad_throttle = Throttle(3.1)


class ItadError(Exception):
    pass


def api_key() -> str:
    return os.getenv("ITAD_API_KEY", "").strip()


def configured() -> bool:
    return bool(api_key())


def country() -> str:
    """The country prices are for: STEAM_COUNTRY, as the store's prices are, or IsThereAnyDeal's default."""
    return (os.getenv("STEAM_COUNTRY", "").strip() or "US").upper()


def _post(path: str, body: list, params: dict | None = None, timeout: float = 30):
    key = api_key()
    if not key:
        raise ItadError("IsThereAnyDeal isn't set up: add ITAD_API_KEY to .env.")
    itad_throttle.wait()
    resp = requests.post(
        f"{API_URL}{path}", json=body, params=params or {}, timeout=timeout,
        headers={"ITAD-API-Key": key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    if resp.status_code == 429:
        raise RateLimited("IsThereAnyDeal asks us to slow down; the next sync carries on.")
    if resp.status_code in (401, 403):
        raise ItadError("IsThereAnyDeal did not accept the ITAD_API_KEY in .env.")
    resp.raise_for_status()
    return resp.json()


def lookup(app_ids: list[int]) -> dict[int, str | None]:
    """Steam app id -> IsThereAnyDeal's game id, or None when it has no game for that app."""
    if len(app_ids) > BATCH:
        raise ValueError(f"Ask for at most {BATCH} games at a time")
    if not app_ids:
        return {}
    answer = _post(f"/lookup/id/shop/{STEAM_SHOP}/v1", [f"app/{int(a)}" for a in app_ids]) or {}
    found = {}
    for app_id in app_ids:
        gid = answer.get(f"app/{int(app_id)}")
        found[app_id] = gid if isinstance(gid, str) and gid else None
    return found


def parse_low(item: dict) -> dict | None:
    """Steam's entry in a store-low answer -> {"cents", "regular", "currency", "cut", "date"}, or None."""
    for low in item.get("lows") or []:
        if not isinstance(low, dict) or (low.get("shop") or {}).get("id") != STEAM_SHOP:
            continue
        price, regular = low.get("price") or {}, low.get("regular") or {}
        if price.get("amountInt") is None or not price.get("currency"):
            return None
        return {
            "cents": int(price["amountInt"]),
            "regular": int(regular["amountInt"]) if regular.get("amountInt") is not None else None,
            "currency": price["currency"],
            "cut": int(low.get("cut") or 0),
            "date": str(low.get("timestamp") or "")[:10] or None,  # "2022-12-21"
        }
    return None


def keep_before(old: dict | None, new: dict, now) -> dict:
    """The new low, remembering the one it replaced. IsThereAnyDeal only knows the lowest price
    ever, so once a sale sets a new record the old low is gone; kept as "before" ({"cents",
    "date"}) with "seen" (the day ShelfLife first saw the new record), it tells a sale that is
    cheaper than ever (prices.low_of) from one that only matches an old low."""
    if not new:
        return {}
    new = dict(new)
    if old and old.get("currency") == new.get("currency") and old.get("cents") is not None:
        if new["cents"] < old["cents"]:
            new["before"] = {"cents": old["cents"], "date": old.get("date")}
            new["seen"] = now.date().isoformat()
        elif new["cents"] == old["cents"]:  # the same low: keep what was remembered about it
            for key in ("before", "seen", "y2", "y2_for", "y2_read"):
                if key in old:
                    new[key] = old[key]
    return new


TWO_YEARS = timedelta(days=730)


def steam_low_since(game_id: str, since: datetime) -> tuple[int, str] | None:
    """Steam's lowest price for one game since `since` (cents, currency), from its price history
    (GET /games/history/v2, Steam only); None when no price changed in that time. One request per
    game, so Sync asks only for games on sale that are not at their lowest ever."""
    key = api_key()
    if not key:
        raise ItadError("IsThereAnyDeal isn't set up: add ITAD_API_KEY to .env.")
    itad_throttle.wait()
    resp = requests.get(
        f"{API_URL}/games/history/v2", timeout=30, headers={"ITAD-API-Key": key, "Accept": "application/json"},
        params={"id": game_id, "country": country(), "shops": STEAM_SHOP, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    if resp.status_code == 429:
        raise RateLimited("IsThereAnyDeal asks us to slow down; the next sync carries on.")
    if resp.status_code in (401, 403):
        raise ItadError("IsThereAnyDeal did not accept the ITAD_API_KEY in .env.")
    resp.raise_for_status()
    seen = [
        (e["deal"]["price"]["amountInt"], e["deal"]["price"]["currency"])
        for e in resp.json() or []
        if isinstance(e, dict) and (e.get("shop") or {}).get("id") == STEAM_SHOP
        and ((e.get("deal") or {}).get("price") or {}).get("amountInt") is not None
    ]
    return min(seen) if seen else None


def steam_lows(game_ids: list[str]) -> dict[str, dict | None]:
    """IsThereAnyDeal game id -> Steam's lowest price ever in your country (parse_low), or None."""
    if len(game_ids) > BATCH:
        raise ValueError(f"Ask for at most {BATCH} games at a time")
    if not game_ids:
        return {}
    answer = _post("/games/storelow/v2", list(game_ids), {"country": country(), "shops": STEAM_SHOP})
    lows: dict[str, dict | None] = {gid: None for gid in game_ids}
    for item in answer or []:
        if isinstance(item, dict) and item.get("id") in lows:
            lows[item["id"]] = parse_low(item)
    return lows
