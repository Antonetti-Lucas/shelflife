"""Explicit ratings, read from data/ratings.csv until the diary exists (Step 6)."""

import csv
from pathlib import Path

RATINGS_PATH = Path("data/ratings.csv")
RATING_COLUMN = "rating_0.5_to_5"


def load_ratings(path: Path = RATINGS_PATH) -> dict[int, float]:
    """steam_app_id -> rating (0.5-5). Blank ratings are skipped."""
    if not path.exists():
        return {}
    ratings: dict[int, float] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = (row.get(RATING_COLUMN) or "").strip()
            if not raw:
                continue
            rating = float(raw)
            if not 0.5 <= rating <= 5:
                raise ValueError(f"Rating out of range for {row['name']!r}: {raw}")
            ratings[int(row["steam_app_id"])] = rating
    return ratings
