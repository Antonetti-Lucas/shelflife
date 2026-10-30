""""Not interested" feedback, kept in data/feedback.csv until the diary exists (Step 6)."""

import csv
from pathlib import Path

FEEDBACK_PATH = Path("data/feedback.csv")
FIELDS = ["steam_app_id", "name", "verdict"]
NOT_INTERESTED = "not_interested"

# In the taste profile a "not interested" game counts like a 2-star rating.
NOT_INTERESTED_RATING = 2.0


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def load_not_interested(path: Path = FEEDBACK_PATH) -> dict[int, str]:
    """steam_app_id -> name for every game marked not interested."""
    return {
        int(r["steam_app_id"]): r["name"]
        for r in _read(path)
        if r["verdict"] == NOT_INTERESTED
    }


def add_not_interested(app_id: int, name: str, path: Path = FEEDBACK_PATH) -> None:
    rows = [r for r in _read(path) if int(r["steam_app_id"]) != app_id]
    rows.append({"steam_app_id": str(app_id), "name": name, "verdict": NOT_INTERESTED})
    _write(rows, path)


def remove(app_id: int, path: Path = FEEDBACK_PATH) -> bool:
    rows = _read(path)
    kept = [r for r in rows if int(r["steam_app_id"]) != app_id]
    if len(kept) == len(rows):
        return False
    _write(kept, path)
    return True
