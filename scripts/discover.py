"""Step 5: build the pool of well-reviewed games you don't own yet.

Run from the project root:
    python -m scripts.discover               # backlog reviews, pool, and details
    python -m scripts.discover --per-tag 25  # smaller pool (default 40 per tag)
    python -m scripts.discover --limit 10    # only fetch details for 10 pool games this run

Fetches only on request, throttled, from Valve's reviews API, SteamSpy and the
Steam store. Safe to interrupt (Ctrl-C) and re-run; finished games are kept.
"""

import argparse
import sys

import requests

from shelflife import diary
from shelflife.db import init_db, session_scope
from shelflife.discovery import build_pool, enrich_pool
from shelflife.models import Game
from shelflife.recommend import BACKLOG_MAX_MIN, top_taste_tags
from shelflife.sync import sync_backlog_reviews
from shelflife.throttle import RateLimited
from shelflife.steam import SteamError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-tag", type=int, default=40, help="pool games to add per tag")
    parser.add_argument("--limit", type=int, help="max pool games to fetch details for")
    args = parser.parse_args()

    init_db()
    with session_scope() as session:
        try:
            n = sync_backlog_reviews(
                session,
                BACKLOG_MAX_MIN,
                on_progress=lambda i, total, g: print(f"[reviews {i}/{total}] {g.name}", flush=True),
            )
            print(f"Review counts fetched for {n} backlog games.\n")

            tags = top_taste_tags(
                session, diary.load_ratings(session), diary.load_not_interested(session)
            )
            print("Building the pool from your taste tags:", ", ".join(tags))
            added = build_pool(session, tags, per_tag=args.per_tag)
            print(f"Added {added} new unowned games.\n")

            def progress(i: int, total: int, game: Game) -> None:
                print(f"[details {i}/{total}] {game.name} ({game.type})", flush=True)

            enrich_pool(session, limit=args.limit, on_progress=progress)
        except RateLimited as exc:
            print(f"\nStopped: {exc} Progress is saved; re-run later.", file=sys.stderr)
            return 2
        except (SteamError, requests.RequestException) as exc:
            print(f"\nStopped: {exc} Progress is saved; re-run to resume.", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nInterrupted. Progress is saved; re-run to resume.")
            return 130
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
