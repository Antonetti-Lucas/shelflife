"""Step 1: print your most-played Steam games.

Run from the project root:  python -m scripts.top_games [N]
"""

import sys

from shelflife.config import load_settings
from shelflife.steam import SteamError, fetch_owned_games


def main() -> int:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    settings = load_settings()

    try:
        games = fetch_owned_games(settings.steam_api_key, settings.steam_id)
    except SteamError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    never_played = sum(1 for g in games if g.playtime_total_min == 0)
    total_hours = sum(g.playtime_total_hours for g in games)
    print(f"{len(games)} games, {total_hours:,.0f}h total, {never_played} never played\n")

    games.sort(key=lambda g: g.playtime_total_min, reverse=True)
    print(f"{'#':>3}  {'Hours':>8}  {'Last 2w':>8}  Game")
    for i, g in enumerate(games[:top_n], start=1):
        print(
            f"{i:>3}  {g.playtime_total_hours:>8.1f}  "
            f"{g.playtime_recent_hours:>8.1f}  {g.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
