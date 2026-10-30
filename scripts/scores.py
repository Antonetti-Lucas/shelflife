"""Step 3: rank your played games by engagement score, with the points breakdown.

Run from the project root:  python -m scripts.scores [N]
"""

import sys

from sqlalchemy import select

from shelflife.db import init_db, session_scope
from shelflife.models import LibraryEntry
from shelflife.score import engagement_score


def main() -> int:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    init_db()

    with session_scope() as session:
        entries = list(
            session.scalars(select(LibraryEntry).where(LibraryEntry.playtime_total_min > 0))
        )
        scored = sorted(
            ((engagement_score(e), e) for e in entries), key=lambda r: r[0].total, reverse=True
        )

        with_ach = sum(1 for e in entries if e.achievements_total)
        print(f"{len(entries)} played games, {with_ach} with achievements\n")
        print(
            f"{'Score':>5}  {'Play':>4} {'Ach':>4} {'Rare':>4} {'Now':>4} {'Rec':>4}  "
            f"{'Hours':>6}  {'Ach':>7}  Game"
        )
        for b, e in scored[:top_n]:
            ach = f"{e.achievements_unlocked}/{e.achievements_total}" if e.achievements_total else "-"
            print(
                f"{b.total:>5.2f}  {b.playtime:>4.1f} {b.achievements:>4.1f} {b.rare:>4.1f} "
                f"{b.still_playing:>4.1f} {b.recency:>+4.1f}  "
                f"{e.playtime_total_hours:>6.1f}  {ach:>7}  {e.game.name}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
