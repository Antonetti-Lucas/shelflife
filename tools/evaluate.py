"""How well does ShelfLife predict what you like? A check to run before and after changing the
scoring, so a change is judged by numbers instead of by a few examples.

    python tools/evaluate.py                  # your own database (only reads it)
    DATABASE_URL=sqlite:///copy.db python tools/evaluate.py --modes mix

For every game you rated, it predicts how much you would like it with everything you told
ShelfLife about that game left out (its rating, favorite and play), from your other games only,
and compares the predictions with your star ratings. If data/rankings.csv exists (your games
ranked best to worst within each year), the ranked games are checked the same way.

The number is a rank correlation (Spearman): +1 means the predictions put your games in exactly
your order, 0 means no better than chance. With one person's games it moves by a few hundredths
between runs of different data, so read small differences lightly.
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])  # the database and data/ are found from the project folder

from shelflife import diary  # noqa: E402
from shelflife.db import SessionLocal  # noqa: E402
from shelflife.rankings import load_rankings  # noqa: E402
from shelflife.recommend import held_out_liking  # noqa: E402
from shelflife.settings import TASTE_MODES  # noqa: E402


def ranks(values: list[float]) -> list[float]:
    """Ranks from 1, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return cov / var if var else float("nan")


def check_ratings(predicted: dict[int, float], ratings: dict[int, float]) -> dict:
    ids = [i for i in predicted if i in ratings]
    preds = [predicted[i] for i in ids]
    stars = [ratings[i] for i in ids]
    by_prediction = sorted(ids, key=predicted.get, reverse=True)
    fifth = max(1, len(ids) // 5)
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
    return {
        "games": len(ids),
        "spearman": spearman(preds, stars),
        "top_fifth": mean([ratings[i] for i in by_prediction[:fifth]]),
        "bottom_fifth": mean([ratings[i] for i in by_prediction[-fifth:]]),
        "average": mean(stars),
    }


def check_rankings(predicted: dict[int, float], ranked: list) -> dict:
    """Rank correlation within each year, averaged with each year weighted by its games."""
    by_year: dict[int, list[tuple[float, int]]] = {}
    for r in ranked:
        if r.app_id in predicted:
            by_year.setdefault(r.year, []).append((predicted[r.app_id], -r.position))  # 1st = best
    years = {y: rows for y, rows in by_year.items() if len(rows) >= 3}
    n = sum(len(rows) for rows in years.values())
    if not n:
        return {"games": 0, "spearman": float("nan"), "years": {}}
    per_year = {y: spearman([p for p, _ in rows], [q for _, q in rows]) for y, rows in sorted(years.items())}
    pooled = sum(per_year[y] * len(rows) for y, rows in years.items()) / n
    return {"games": n, "spearman": pooled, "years": {y: (v, len(years[y])) for y, v in per_year.items()}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modes", default=",".join(TASTE_MODES), help="taste modes to check, comma separated")
    args = parser.parse_args()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in TASTE_MODES]
    if unknown:
        parser.error(f"unknown mode(s) {', '.join(unknown)}; choose from {', '.join(TASTE_MODES)}")

    ranked = [r for r in load_rankings() if r.app_id]
    with SessionLocal() as session:
        ratings = diary.load_ratings(session)
        not_interested = diary.load_not_interested(session)
        print(f"{len(ratings)} rated games" + (f", {len(ranked)} ranked games" if ranked else "") + "\n")
        for mode in modes:
            started = time.perf_counter()
            wanted = set(ratings) | {r.app_id for r in ranked}
            predicted = held_out_liking(session, ratings, not_interested, wanted, mode=mode)
            r = check_ratings(predicted, ratings)
            line = (
                f"{TASTE_MODES[mode]:<18} ratings: {r['spearman']:+.3f} on {r['games']} games "
                f"(top fifth averages {r['top_fifth']:.2f}★, bottom fifth {r['bottom_fifth']:.2f}★, all {r['average']:.2f}★)"
            )
            if ranked:
                k = check_rankings(predicted, ranked)
                if k["games"]:
                    years = ", ".join(f"{y} {v:+.2f} (n={c})" for y, (v, c) in k["years"].items())
                    line += f"\n{'':<18} rankings: {k['spearman']:+.3f} on {k['games']} games [{years}]"
                else:
                    line += f"\n{'':<18} rankings: no year has 3 ranked games with tags in this database"
            print(line + f"  [{time.perf_counter() - started:.0f}s]\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
