"""Step 3: fetch your achievement progress for every game you've played.

Run from the project root:  python -m scripts.achievements

Uses only the official Steam Web API (about two calls per game with achievements).
Safe to interrupt (Ctrl-C) and re-run: finished games are only re-fetched if you
played them in the last two weeks.
"""

import sys

from shelflife.achievements import AchievementStats
from shelflife.config import load_settings
from shelflife.db import init_db, session_scope
from shelflife.models import Game
from shelflife.steam import SteamError, fetch_owned_games
from shelflife.sync import sync_achievements, upsert_library


def _progress(i: int, total: int, game: Game, stats: AchievementStats) -> None:
    detail = f"{stats.unlocked}/{stats.total}" if stats.total else "no achievements"
    print(f"[{i}/{total}] {game.name}: {detail}", flush=True)


def main() -> int:
    settings = load_settings()
    init_db()

    with session_scope() as session:
        try:
            # Refresh playtime first so "played recently" is current.
            upsert_library(session, fetch_owned_games(settings.steam_api_key, settings.steam_id))
            sync_achievements(
                session, settings.steam_api_key, settings.steam_id, on_progress=_progress
            )
        except SteamError as exc:
            print(f"\nStopped: {exc} Progress is saved; re-run later.", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nInterrupted. Progress is saved; re-run to resume.")
            return 130
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
