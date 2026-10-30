"""Bring your ratings and "not interested" marks from the old CSV files into the diary.

Run from the project root:  python -m scripts.import_diary

Reads data/ratings.csv and data/feedback.csv. Safe to run more than once: nothing
already in the diary is overwritten. After this, rate games in the web app instead.
"""

from shelflife.db import init_db, session_scope
from shelflife.diary import import_legacy_csv


def main() -> int:
    init_db()
    with session_scope() as session:
        rated, marked = import_legacy_csv(session)
    print(f"Imported {rated} ratings and {marked} 'not interested' marks into the diary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
