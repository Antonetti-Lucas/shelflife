"""Hours you enter by hand, for games Steam reports no playtime for (family games).

Steam shows these hours in your client but not through its Web API, so you copy
them into data/hours.csv. The numbers become the game's playtime, which feeds the
engagement score and the "have you played it" check.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife import diary
from shelflife.models import Game, LibraryEntry
from shelflife.rankings import load_rankings

HOURS_PATH = Path("data/hours.csv")
FIELDS = ["steam_app_id", "name", "hours", "achievements", "ranked"]


@dataclass(frozen=True)
class HoursResult:
    applied: list[tuple[Game, float]]
    owned_skipped: list[Game]  # Steam already reports hours for these
    unknown: list[int]  # not in your library or family list


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Rows of the CSV; accepts ';' as the separator (Excel in some locales)."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        header = f.readline()
        f.seek(0)
        delimiter = ";" if header.count(";") > header.count(",") else ","
        return list(csv.DictReader(f, delimiter=delimiter))


def load_hours(path: Path = HOURS_PATH) -> dict[int, float]:
    """steam_app_id -> hours. Blank hours are skipped; '12,5' and '12.5' both work."""
    if not path.exists():
        return {}
    hours: dict[int, float] = {}
    for row in _read_rows(path):
        raw = (row.get("hours") or "").strip().replace(",", ".")
        if not raw:
            continue
        value = float(raw)
        if value < 0:
            raise ValueError(f"Negative hours for {row.get('name')!r}: {raw}")
        hours[int(row["steam_app_id"])] = value
    return hours


def apply_hours(session: Session, hours: dict[int, float]) -> HoursResult:
    """Set playtime for family games from the entered hours."""
    applied: list[tuple[Game, float]] = []
    owned_skipped: list[Game] = []
    unknown: list[int] = []
    for app_id, value in hours.items():
        game = session.get(Game, app_id)
        entry = session.get(LibraryEntry, app_id)
        if game is None or entry is None:
            unknown.append(app_id)
        elif not game.family_shared:
            owned_skipped.append(game)
        else:
            entry.playtime_total_min = round(value * 60)
            applied.append((game, value))
    session.commit()
    return HoursResult(applied, owned_skipped, unknown)


def set_hours(session: Session, app_id: int, hours: float | None) -> None:
    """Set the hours for one family game; None or 0 clears them.

    Only family games take hand-entered hours: for games you own, the next Steam
    sync would overwrite them with Steam's own number.
    """
    game = session.get(Game, app_id)
    entry = session.get(LibraryEntry, app_id)
    if game is None or entry is None or not game.family_shared:
        raise ValueError("Hours can only be entered for family games; Steam reports them for games you own.")
    if hours is not None and not 0 <= hours <= 100_000:
        raise ValueError(f"Hours must be between 0 and 100000, got {hours}")
    entry.playtime_total_min = round((hours or 0) * 60)
    session.commit()


def write_template(session: Session, path: Path = HOURS_PATH) -> int:
    """Create or extend the hours file with family games you've probably played.

    A game is listed if you unlocked an achievement in it, ranked it, or rated it.
    Hours you already entered are kept. Returns the number of rows added.
    """
    existing = _read_rows(path) if path.exists() else []
    known = {int(r["steam_app_id"]) for r in existing}

    ranked = {r.app_id: r for r in load_rankings() if r.app_id}
    rated = set(diary.load_ratings(session))

    candidates: list[tuple[tuple, Game]] = []
    for game in session.scalars(select(Game).where(Game.family_shared.is_(True))):
        entry = game.library_entry
        unlocked = (entry.achievements_unlocked or 0) if entry else 0
        rank = ranked.get(game.steam_app_id)
        if game.steam_app_id in known or not (unlocked or rank or game.steam_app_id in rated):
            continue
        # Ranked games first (newest year, best position), then by achievements.
        order = (0, -rank.year, rank.position) if rank else (1, -unlocked, 0)
        candidates.append((order, game))
    candidates.sort(key=lambda c: c[0])

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in existing:
            writer.writerow({k: row.get(k, "") for k in FIELDS})
        for _, game in candidates:
            entry = game.library_entry
            rank = ranked.get(game.steam_app_id)
            writer.writerow(
                {
                    "steam_app_id": game.steam_app_id,
                    "name": game.name,
                    "hours": "",
                    "achievements": (
                        f"{entry.achievements_unlocked}/{entry.achievements_total}"
                        if entry and entry.achievements_total
                        else ""
                    ),
                    "ranked": f"{rank.year} #{rank.position}" if rank else "",
                }
            )
    return len(candidates)
