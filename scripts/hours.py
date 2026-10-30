"""Enter playtime by hand for family games (Steam's Web API doesn't report it).

Run from the project root:
    python -m scripts.hours --template   # list family games you've probably played in data/hours.csv
    python -m scripts.hours              # apply the hours you filled in

Open data/hours.csv, type the hours your Steam client shows for each game in the
"hours" column (e.g. 24.5), save, then run the second command. Leave a row blank
to skip it. Games you own are ignored: Steam already reports their hours.
"""

import argparse

from shelflife.db import init_db, session_scope
from shelflife.hours import HOURS_PATH, apply_hours, load_hours, write_template


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", action="store_true", help="create/extend data/hours.csv")
    args = parser.parse_args()

    init_db()
    with session_scope() as session:
        if args.template:
            added = write_template(session)
            print(f"Added {added} games to {HOURS_PATH}. Fill in the hours column, then run:")
            print("  python -m scripts.hours")
            return 0

        hours = load_hours()
        if not hours:
            print(f"No hours found in {HOURS_PATH}. Run with --template to create it.")
            return 1
        result = apply_hours(session, hours)

        for game, value in result.applied:
            print(f"  {game.name}: {value:g}h")
        print(f"\nApplied hours to {len(result.applied)} family games.")
        if result.owned_skipped:
            names = ", ".join(g.name for g in result.owned_skipped)
            print(f"Skipped (you own them, Steam reports their hours): {names}")
        if result.unknown:
            print(f"Skipped (not in your library or family list): {result.unknown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
