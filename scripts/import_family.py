"""Import your Steam Family Sharing library.

Steps:
  1. Run tools/export_family_library.js in your logged-in browser (see the file).
  2. Put the downloaded family-library.json in data/.
  3. Run from the project root:  python -m scripts.import_family [path]

Afterwards, fetch details and achievements for the new games:
    python -m scripts.sync --limit 100     # tags and store details, most useful first
    python -m scripts.achievements         # shows which family games you've played

Re-running syncs the list: games no longer shared with you are removed.
"""

import argparse
import sys
from pathlib import Path

from shelflife.appnames import fetch_app_names
from shelflife.config import load_settings
from shelflife.db import init_db, session_scope
from shelflife.family import EXPORT_PATH, import_family, load_export, shared_with_you
from shelflife.models import Game
from shelflife.rankings import link_rankings, load_rankings, save_rankings, suggest_links
from shelflife.steam import SteamError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, default=EXPORT_PATH, help="export file")
    args = parser.parse_args()

    settings = load_settings()
    if not args.path.exists():
        print(f"{args.path} not found. Create it with tools/export_family_library.js.", file=sys.stderr)
        return 1
    try:
        owner, apps = load_export(args.path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if owner and owner != settings.steam_id:
        print(
            f"Warning: the export is for SteamID {owner}, but STEAM_ID in .env is "
            f"{settings.steam_id}. Using the one in .env.",
            file=sys.stderr,
        )

    shared = shared_with_you(apps, settings.steam_id)
    print(
        f"{len(apps)} apps in the export: {len(shared)} shared with you, "
        f"{sum(1 for a in apps if settings.steam_id in a.owner_steamids)} owned by you, "
        f"{sum(1 for a in apps if a.exclude_reason != 0)} unavailable."
    )

    init_db()
    with session_scope() as session:
        unnamed = {a.app_id for a in shared if not a.name and session.get(Game, a.app_id) is None}
        try:
            names = fetch_app_names(settings.steam_api_key, unnamed)
        except SteamError as exc:
            print(f"Could not look up game names ({exc}); using placeholders for now.", file=sys.stderr)
            names = {}

        result = import_family(session, shared, names)
        print(
            f"Added {result.added} family games ({result.already_known} already imported, "
            f"{result.pruned} no longer shared)."
        )

        rankings = load_rankings()
        linked = link_rankings(session, rankings)
        if linked:
            save_rankings(rankings)
            print(f"Linked {len(linked)} ranked games to app ids:")
            for r in linked:
                game = session.get(Game, r.app_id)
                print(f"  {r.year} #{r.position}: {r.name}  ->  {game.name} ({r.app_id})")
        for i, games in suggest_links(session, rankings).items():
            r = rankings[i]
            options = "; ".join(f"{g.name} ({g.steam_app_id})" for g in games)
            print(f"  Unsure: {r.year} #{r.position} {r.name!r} could be: {options}")
        print(
            "  To link a game yourself, put its app id in the steam_app_id column of data/rankings.csv."
        )
    print("\nNext: python -m scripts.sync --limit 100  and  python -m scripts.achievements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
