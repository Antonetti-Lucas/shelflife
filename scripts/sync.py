"""Step 2: save your Steam library to SQLite and fetch store details + tags.

Run from the project root:
    python -m scripts.sync              # library + details for every new game
    python -m scripts.sync --limit 5    # only enrich the 5 most-played new games
    python -m scripts.sync --no-details # library only

Safe to interrupt (Ctrl-C) and re-run: finished games are never fetched again.
"""

import argparse
import sys

import requests
from sqlalchemy import func, select

from shelflife.config import load_settings
from shelflife.db import init_db, session_scope
from shelflife.models import Game
from shelflife.steam import SteamError, fetch_owned_games
from shelflife.sync import backfill_empty_tags, enrich_games, upsert_library
from shelflife.throttle import RateLimited


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, help="max games to enrich this run")
    parser.add_argument("--no-details", action="store_true", help="skip store/tag fetch")
    parser.add_argument(
        "--fill-tags", action="store_true",
        help="retry games with no tags using the store page, then exit",
    )
    args = parser.parse_args()

    if args.fill_tags:
        init_db()
        with session_scope() as session:
            try:
                filled = backfill_empty_tags(
                    session,
                    on_progress=lambda i, n, g: print(
                        f"[{i}/{n}] {g.name}: {len(g.tags)} tags", flush=True
                    ),
                )
            except RateLimited as exc:
                print(f"\nStopped: {exc} Re-run later.", file=sys.stderr)
                return 2
        print(f"\nFilled tags for {filled} games.")
        return 0

    settings = load_settings()
    init_db()

    try:
        owned = fetch_owned_games(settings.steam_api_key, settings.steam_id)
    except SteamError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    with session_scope() as session:
        upsert_library(session, owned)
        print(f"Saved {len(owned)} library entries.")

        if not args.no_details:
            def progress(i: int, total: int, game: Game) -> None:
                tag_names = ", ".join(sorted(game.tags, key=game.tags.get, reverse=True)[:3])
                print(f"[{i}/{total}] {game.name} ({game.type}) {tag_names}", flush=True)

            try:
                enrich_games(session, limit=args.limit, on_progress=progress)
            except RateLimited as exc:
                print(f"\nStopped: {exc} Progress is saved; re-run later.", file=sys.stderr)
                return 2
            except requests.RequestException as exc:
                print(f"\nNetwork error: {exc}. Progress is saved; re-run to resume.", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                print("\nInterrupted. Progress is saved; re-run to resume.")
                return 130

        total = session.scalar(select(func.count()).select_from(Game))
        missing = session.scalar(
            select(func.count()).select_from(Game).where(Game.details_fetched_at.is_(None))
        )
        by_type = session.execute(
            select(Game.type, func.count()).group_by(Game.type).order_by(func.count().desc())
        ).all()
        print(f"\n{total} games in database, {missing} still waiting for details.")
        print("By type:", ", ".join(f"{t or 'unknown'}={n}" for t, n in by_type))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
