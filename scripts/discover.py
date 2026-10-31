"""Step 5: find well-reviewed games you don't own yet.

Run from the project root:
    python -m scripts.discover                       # games for the tags you like most
    python -m scripts.discover --per-tag 20          # 20 new games per tag (default 10)
    python -m scripts.discover --tags "RPG,Co-op"    # search these tags instead

Each tag's best-reviewed games are read until enough are really about the tag (it
must be one of the game's 10 most voted tags), and running it again finds new ones.
The Explore page has the same thing as a button. Fetches only on request, throttled,
from Valve's reviews API, SteamSpy and the Steam store. Safe to interrupt (Ctrl-C)
and re-run; finished games are kept.
"""

import argparse
import sys

import requests

from shelflife import diary
from shelflife.db import init_db, session_scope
from shelflife.discovery import MIN_REVIEWS, find_games
from shelflife.recommend import BACKLOG_MAX_MIN, top_taste_tags
from shelflife.steam import SteamError
from shelflife.sync import sync_backlog_reviews
from shelflife.tagfilter import parse_tags
from shelflife.throttle import RateLimited


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-tag", type=int, default=10, help="new games to find per tag")
    parser.add_argument("--tags", help="comma-separated tags to search (default: the tags you like most)")
    parser.add_argument("--min-reviews", type=int, default=MIN_REVIEWS, help="skip games with fewer reviews")
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

            tags = parse_tags(args.tags) or top_taste_tags(
                session, diary.load_ratings(session), diary.load_not_interested(session)
            )
            print("Looking for games in the tags:", ", ".join(tags))

            def progress(phase: str, done: int, total: int, text: str) -> None:
                if text:
                    print(f"[{phase} {done}/{total}] {text}", flush=True)

            result = find_games(
                session, tags, args.per_tag * len(tags), args.min_reviews, on_progress=progress
            )
            print(
                f"\nAdded {result.added} new games (looked at {result.checked}, set aside "
                f"{result.skipped} that only mention a tag in passing); "
                f"fetched details for {result.enriched}."
            )
            if result.empty_tags:
                print("SteamSpy lists no games for:", ", ".join(result.empty_tags), "(check the spelling)")
            if result.aborted:
                print(f"Stopped early: {result.aborted} Progress is saved; re-run later.", file=sys.stderr)
                return 2
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
