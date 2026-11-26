"""How close the tags made from IGDB come to real Steam tags (shelflife/igdb.py, mapped_tags).

Takes Steam games from your library that IGDB also has, makes their tags from IGDB's data the
way a game that is not on Steam gets them, and compares them with the games' real Steam tags
(cosine of the tag vectors the taste uses). A random other game gives the scale. Only reads the
database; IGDB's answers are not stored. Needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in .env.

    python tools/check_igdb_tags.py copy.db              # your rated games and 80 others
    python tools/check_igdb_tags.py copy.db --others 200
"""

import argparse
import json
import random
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shelflife import igdb  # noqa: E402
from shelflife.tags import compute_idf, cosine, tag_vector  # noqa: E402

BATCH = 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("database", help="a ShelfLife database (a copy is safest; it is only read)")
    parser.add_argument("--others", type=int, default=80, help="unrated library games to add to the sample")
    args = parser.parse_args()
    if not igdb.configured():
        print("IGDB isn't set up: add IGDB_CLIENT_ID and IGDB_CLIENT_SECRET to .env.")
        return 1

    db = sqlite3.connect(f"file:{Path(args.database).as_posix()}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT g.steam_app_id, g.name, g.tags, d.rating FROM games g"
        " JOIN library_entries l ON l.steam_app_id = g.steam_app_id"
        " LEFT JOIN diary_entries d ON d.steam_app_id = g.steam_app_id"
        " WHERE g.type = 'game' AND g.tags != '{}' AND g.steam_app_id > 0"
    ).fetchall()
    library = {app: (name, json.loads(tags), rating) for app, name, tags, rating in rows}
    known: set[str] = set()
    for (tags,) in db.execute("SELECT tags FROM games WHERE type = 'game' AND steam_app_id > 0"):
        known.update(json.loads(tags or "{}"))
    idf = compute_idf(tags for _, tags, _ in library.values())

    rated = [app for app, (_, _, rating) in library.items() if rating is not None]
    rest = [app for app in library if app not in rated]
    random.seed(1)
    sample = rated + random.sample(rest, min(args.others, len(rest)))

    links: dict[int, int] = {}  # Steam id -> IGDB id
    for i in range(0, len(sample), BATCH):
        uids = ",".join(f'"{app}"' for app in sample[i : i + BATCH])
        for e in igdb.query("external_games", f"fields game,uid; where external_game_source = {igdb.STEAM_SOURCE} & uid = ({uids}); limit 500;"):
            links.setdefault(int(e["uid"]), e["game"])
    games: dict[int, igdb.IgdbGame] = {}
    ids = sorted(set(links.values()))
    for i in range(0, len(ids), BATCH):
        for item in igdb.query("games", f"fields {igdb.FIELDS}; where id = ({','.join(map(str, ids[i : i + BATCH]))}); limit 500;"):
            games[item["id"]] = igdb.parse(item)

    apps = [app for app in sample if links.get(app) in games]
    if len(apps) < 4:
        print(f"IGDB has only {len(apps)} of the {len(sample)} games: too few to tell.")
        return 1
    alike, chance, counts, texts, lows = [], [], [], [], []
    for app in apps:
        real = tag_vector(library[app][1], idf)
        made = igdb.mapped_tags(games[links[app]], known)
        score = cosine(real, tag_vector(igdb.votes_for(made), idf))
        alike.append(score)
        counts.append(len(made))
        texts.append(len(games[links[app]].text))
        lows.append((score, library[app][0], made[:8]))
        other = random.choice([b for b in library if b != app])
        chance.append(cosine(real, tag_vector(library[other][1], idf)))

    def quartiles(xs):
        return ", ".join(f"{q:.2f}" for q in statistics.quantiles(xs, n=4))

    print(f"{len(sample)} library games, IGDB has {len(apps)}")
    print(f"tags made from IGDB vs the real Steam tags: median {statistics.median(alike):.3f} (quartiles {quartiles(alike)})")
    print(f"a random other game vs the real tags:     median {statistics.median(chance):.3f} (quartiles {quartiles(chance)})")
    print(f"tags made per game: median {statistics.median(counts):g}; IGDB text: median {statistics.median(texts):g} characters, {sum(1 for t in texts if not t)} empty")
    print("furthest off:")
    for score, name, made in sorted(lows)[:5]:
        print(f"  {score:.2f}  {name}: {', '.join(made)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
