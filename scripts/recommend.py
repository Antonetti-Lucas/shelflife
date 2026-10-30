"""Recommend games from your backlog and from outside your library.

Run from the project root:  python -m scripts.recommend [N]

Ratings and "not interested" marks come from your diary (the web app writes
them; `python -m scripts.import_diary` brings in the old CSV files). Unrated games
count through their engagement score. Run `python -m scripts.discover` first to
fill the "to discover" list.
"""

import sys

from shelflife import diary
from shelflife.db import init_db, session_scope
from shelflife.recommend import Recommendation, recommend


def _reviews(rec: Recommendation) -> str:
    pos, neg = rec.game.reviews_positive, rec.game.reviews_negative
    if pos is None or neg is None or pos + neg == 0:
        return "no review data"
    return f"{pos / (pos + neg):.0%} positive, {pos + neg:,} reviews"


def _print(recs: list[Recommendation]) -> None:
    for i, r in enumerate(recs, start=1):
        print(f"{i:>2}. {r.game.name}  (score {r.score:.2f} = {r.similarity:.2f} match x {r.quality:.2f} quality)")
        print(f"    {_reviews(r)}")
        if r.because:
            print(f"    because you played: {', '.join(r.because)}")
        if r.tags:
            print(f"    matching tags: {', '.join(r.tags)}")
        if r.watch:
            print(f"    note: {', '.join(r.watch)} tends to score lower for you")


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    init_db()

    with session_scope() as session:
        result = recommend(
            session, diary.load_ratings(session), diary.load_not_interested(session), limit
        )
        taste = result.taste

        print(
            f"Taste profile from {taste.explicit_count} rated and "
            f"{taste.implicit_count} unrated (engagement-based) games"
        )
        ranked = sorted(taste.profile.items(), key=lambda kv: kv[1], reverse=True)
        print("  leans toward:", ", ".join(t for t, _ in ranked[:8]))
        print("  leans away from:", ", ".join(t for t, v in ranked[::-1][:5] if v < 0) or "-")

        print(f"\n=== From your backlog (top {len(result.backlog)}) ===\n")
        _print(result.backlog)

        if result.family:
            print(f"\n=== From your family library, free to play (top {len(result.family)}) ===\n")
            _print(result.family)

        print(f"\n=== To discover: not in your library (top {len(result.discover)}) ===\n")
        if result.discover:
            _print(result.discover)
        else:
            print("Nothing yet. Run `python -m scripts.discover` to build the pool.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
