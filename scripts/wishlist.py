"""Read your Steam wishlist into the database.

Run from the project root:
    python -m scripts.wishlist

Needs only your SteamID (no API key), and the wishlist must be public on Steam. New games on it
get their store details with `python -m scripts.sync`, and the web app's Sync button does both.
Wishlisting never changes your taste profile.
"""

import sys

import requests

from shelflife.config import load_settings
from shelflife.db import init_db, session_scope
from shelflife.throttle import RateLimited
from shelflife.wishlist import fetch_wishlist, import_wishlist


def main() -> int:
    settings = load_settings()
    init_db()
    try:
        entries = fetch_wishlist(settings.steam_id)
    except RateLimited as exc:
        print(f"Stopped: {exc} Run it again later.", file=sys.stderr)
        return 2
    except (requests.RequestException, ValueError) as exc:
        print(f"Could not read your wishlist: {exc}", file=sys.stderr)
        return 1

    with session_scope() as session:
        result = import_wishlist(session, entries)
    if result.kept_old_list:
        print(f"Steam returned an empty wishlist (is it private?), so your earlier one was kept ({result.total} games).")
        return 0
    print(f"{result.total} games on your wishlist: {result.added} new, {result.removed} no longer on it.")
    if result.added:
        print("Run `python -m scripts.sync` to fetch their store details.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
