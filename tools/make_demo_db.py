"""Build a demo database of popular Steam games, for README screenshots.

    python tools/make_demo_db.py demo.db
    DATABASE_URL=sqlite:///demo.db python -m scripts.web --port 8001

Game names, covers, tags and review counts are real (fetched from Steam and
SteamSpy, so this needs a network connection and takes a few minutes). The
playtime, achievements and ratings are made up for an imaginary player. It only
writes the file you name and never touches your own database.
"""

import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if len(sys.argv) != 2 or "demo" not in Path(sys.argv[1]).name:
    sys.exit("Usage: python tools/make_demo_db.py <path containing 'demo', e.g. demo.db>")
DEMO_PATH = Path(sys.argv[1]).resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{DEMO_PATH.as_posix()}"  # before shelflife is imported
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from shelflife import diary  # noqa: E402
from shelflife.db import SessionLocal, init_db  # noqa: E402
from shelflife.models import Game, LibraryEntry, WishlistItem  # noqa: E402
from shelflife.sync import enrich_one  # noqa: E402

NOW = datetime.now(timezone.utc).replace(tzinfo=None)
RNG = random.Random(7)

# app id: (hours, share of achievements unlocked, first unlock years ago, last unlock years ago, minutes in last 2 weeks)
PLAYED = {
    292030: (142, 0.85, 4.0, 3.6, 0),  # The Witcher 3
    1091500: (96, 0.70, 2.5, 1.9, 0),  # Cyberpunk 2077
    1245620: (188, 0.60, 1.8, 0.1, 640),  # Elden Ring
    1145360: (71, 0.90, 3.2, 2.0, 0),  # Hades
    413150: (120, 0.55, 5.0, 0.1, 240),  # Stardew Valley
    105600: (210, 0.40, 6.0, 4.5, 0),  # Terraria
    620: (18, 1.00, 7.0, 6.9, 0),  # Portal 2
    489830: (260, 0.45, 5.5, 3.0, 0),  # Skyrim Special Edition
    367520: (48, 0.75, 4.0, 2.8, 0),  # Hollow Knight
    504230: (22, 0.80, 3.0, 2.9, 0),  # Celeste
    646570: (130, 0.50, 4.0, 1.0, 0),  # Slay the Spire
    632360: (95, 0.35, 3.0, 0.5, 120),  # Risk of Rain 2
    374320: (80, 0.90, 6.0, 5.0, 0),  # Dark Souls III
    632470: (35, 0.90, 3.0, 2.9, 0),  # Disco Elysium
    268910: (28, 0.90, 5.0, 4.8, 0),  # Cuphead
    264710: (40, 0.80, 3.0, 2.7, 0),  # Subnautica
    427520: (300, 0.30, 4.0, 0.2, 600),  # Factorio
    550: (110, 0.60, 9.0, 6.0, 0),  # Left 4 Dead 2
    548430: (160, 0.40, 3.0, 0.1, 360),  # Deep Rock Galactic
    1659040: (75, 0.55, 3.5, 1.5, 0),  # HITMAN World of Assassination
    22380: (90, 0.70, 8.0, 7.0, 0),  # Fallout: New Vegas
}
# Owned but barely played: app id -> minutes
BACKLOG = {1086940: 20, 1174180: 0, 814380: 35, 753640: 0, 782330: 10, 2050650: 0, 2379780: 0,
           1794680: 25, 1966720: 0, 526870: 0}  # BG3, RDR2, Sekiro, Outer Wilds, DOOM Eternal, RE4, Balatro, Vampire Survivors, Lethal Company, Satisfactory
FAMILY = [1145350, 1426210, 990080, 553850, 1623730]  # Hades II, It Takes Two, Hogwarts Legacy, Helldivers 2, Palworld
POOL = [730, 570, 400, 220, 391540, 289070, 268500, 294100, 255710, 1030300, 1868140, 1229490, 1604030,
        1817070, 1593500, 1888930, 1196590, 1151640, 322330, 381210, 261570, 1057090, 304430, 48000,
        219150, 460950, 588650, 311690, 250900, 418530, 653530, 200260, 238960, 1172620, 960090]
RATINGS = {  # app id: (rating, review)
    292030: (5.0, "Best story I've played in years."), 1145360: (5.0, "Perfect run after run."),
    1245620: (4.5, ""), 413150: (4.5, "My comfort game."), 620: (5.0, ""), 367520: (4.5, ""),
    504230: (4.0, ""), 268910: (3.5, "Gorgeous, but too hard for me."), 489830: (4.0, ""),
    105600: (3.5, ""), 550: (3.0, ""), 1091500: (4.0, "Great after the patches."),
    646570: (4.5, "Perfect deckbuilder."), 374320: (4.5, ""),
}
NOT_INTERESTED = [570]  # Dota 2
# Where the imaginary player is with a game: app id -> (status, started, finished); dates are made up
STATUSES = {
    1245620: ("playing", date(2026, 9, 2), None),  # Elden Ring
    413150: ("playing", date(2025, 3, 14), None),  # Stardew Valley
    427520: ("playing", date(2026, 1, 8), None),  # Factorio
    292030: ("played", date(2022, 11, 2), date(2023, 2, 20)),  # The Witcher 3
    1145360: ("played", date(2023, 5, 1), date(2023, 7, 9)),  # Hades
    620: ("played", date(2019, 1, 5), date(2019, 1, 9)),  # Portal 2
    367520: ("played", date(2024, 2, 3), date(2024, 3, 1)),  # Hollow Knight
    504230: ("played", date(2024, 6, 10), date(2024, 6, 14)),  # Celeste
    632470: ("played", date(2024, 9, 1), date(2024, 10, 12)),  # Disco Elysium
    646570: ("played", date(2025, 1, 4), date(2025, 2, 2)),  # Slay the Spire
    1091500: ("played", date(2025, 6, 6), date(2025, 7, 30)),  # Cyberpunk 2077
    268910: ("dropped", date(2023, 8, 1), date(2023, 8, 20)),  # Cuphead
    374320: ("dropped", date(2022, 4, 1), date(2022, 5, 15)),  # Dark Souls III
}
FAVORITES = [1145360, 292030, 413150]  # Hades, The Witcher 3, Stardew Valley
# Unowned games on the imaginary player's wishlist: app id -> (your rank or 0, days ago added)
WISHLIST = {
    1868140: (1, 400), 1229490: (2, 90), 1604030: (0, 30), 1817070: (3, 700), 1593500: (0, 15),
    1888930: (0, 200), 1196590: (4, 55), 1151640: (0, 8), 1172620: (0, 300),
}


def global_achievements(app_id):
    """Real achievement unlock rates for a game (public endpoint, no key)."""
    url = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/"
    resp = requests.get(url, params={"gameid": app_id}, timeout=20)
    if resp.status_code != 200:
        return []
    rows = resp.json().get("achievementpercentages", {}).get("achievements", [])
    return sorted((float(r["percent"]) for r in rows), reverse=True)


def stamp(years_ago):
    return int((NOW - timedelta(days=years_ago * 365.25)).replace(tzinfo=timezone.utc).timestamp())


def main():
    if DEMO_PATH.exists():
        DEMO_PATH.unlink()
    DEMO_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()

    with SessionLocal() as s:
        def add(app_id, owned, family=False, minutes=0):
            s.add(Game(steam_app_id=app_id, name=f"App {app_id}", family_shared=True if family else (False if owned else None)))
            if owned or family:
                s.add(LibraryEntry(steam_app_id=app_id, playtime_total_min=minutes, playtime_recent_min=0, last_synced=NOW))

        for app_id, (hours, *_rest) in PLAYED.items():
            add(app_id, True, minutes=hours * 60)
        for app_id, minutes in BACKLOG.items():
            add(app_id, True, minutes=minutes)
        for app_id in FAMILY:
            add(app_id, False, family=True)
        for app_id in POOL:
            add(app_id, False)
        s.commit()

        games = s.query(Game).all()
        for i, game in enumerate(games, start=1):
            try:
                enrich_one(game)
                s.commit()
                print(f"[{i}/{len(games)}] {game.name} ({game.type}, {len(game.tags or {})} tags)", flush=True)
            except Exception as exc:  # keep going: a game that fails is dropped below
                s.rollback()
                print(f"[{i}/{len(games)}] app {game.steam_app_id} FAILED: {exc}", flush=True)

        # Keep only real games with tags and a name.
        for game in s.query(Game).all():
            if game.type != "game" or not game.tags or game.name.startswith("App "):
                print(f"dropping app {game.steam_app_id} ({game.name}): type={game.type}, tags={len(game.tags or {})}")
                if game.library_entry:
                    s.delete(game.library_entry)
                s.delete(game)
        s.commit()

        # Made-up play history, using each game's real achievement list.
        for app_id, (hours, share, first, last, recent) in PLAYED.items():
            entry = s.get(LibraryEntry, app_id)
            if entry is None:
                continue
            rates = global_achievements(app_id)
            time.sleep(0.3)
            unlocked = round(len(rates) * share)
            entry.playtime_recent_min = recent
            entry.last_played = NOW if recent else NOW - timedelta(days=last * 365.25)
            entry.achievements_total = len(rates)
            entry.achievements_unlocked = unlocked
            entry.achievements_fetched_at = NOW
            if unlocked:
                entry.rarest_unlocked_pct = rates[unlocked - 1]  # you hold the most common ones first
                times = sorted(stamp(last + (first - last) * RNG.random()) for _ in range(unlocked))
                entry.unlock_times = times
                entry.first_unlock_at = datetime.fromtimestamp(times[0], timezone.utc).replace(tzinfo=None)
                entry.last_unlock_at = datetime.fromtimestamp(times[-1], timezone.utc).replace(tzinfo=None)
        s.commit()

        for app_id, (rating, review) in RATINGS.items():
            if s.get(Game, app_id):
                diary.save_rating(s, app_id, rating, review or None)
        for app_id in NOT_INTERESTED:
            if s.get(Game, app_id):
                diary.set_not_interested(s, app_id, True)

        for app_id, (status, started, finished) in STATUSES.items():
            if s.get(Game, app_id):
                diary.set_status(s, app_id, status, started, finished)
        for app_id in FAVORITES:
            if s.get(Game, app_id):
                diary.set_favorite(s, app_id, True)
        for app_id, (rank, days_ago) in WISHLIST.items():
            if s.get(Game, app_id):
                s.add(WishlistItem(steam_app_id=app_id, priority=rank, added_at=NOW - timedelta(days=days_ago), seen_at=NOW))
        s.commit()

        print(f"\nDemo database ready: {DEMO_PATH}")
        print(f"games: {s.query(Game).count()} | library entries: {s.query(LibraryEntry).count()} | ratings: {len(diary.load_ratings(s))}")


if __name__ == "__main__":
    main()
