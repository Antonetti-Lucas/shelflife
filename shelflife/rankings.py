"""Yearly rankings (best to worst) from data/rankings.csv, linked to games by title."""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.models import Game

RANKINGS_PATH = Path("data/rankings.csv")
FIELDS = ["year", "position", "name", "steam_app_id"]


@dataclass
class RankedGame:
    year: int
    position: int
    name: str
    app_id: int | None


def _norm(name: str) -> str:
    name = name.lower().replace("™", "").replace("®", "").replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def load_rankings(path: Path = RANKINGS_PATH) -> list[RankedGame]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [
            RankedGame(
                year=int(r["year"]),
                position=int(r["position"]),
                name=r["name"],
                app_id=int(r["steam_app_id"]) if r["steam_app_id"] else None,
            )
            for r in csv.DictReader(f)
        ]


def save_rankings(rows: list[RankedGame], path: Path = RANKINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        for r in rows:
            writer.writerow([r.year, r.position, r.name, r.app_id or ""])


def _extends(title: str, ranked: str) -> bool:
    """True when title is the ranked title plus more words (an edition, a subtitle)."""
    return title.startswith(ranked + " ")


def link_rankings(session: Session, rows: list[RankedGame]) -> list[RankedGame]:
    """Fill in app ids for ranked games whose title matches a game we know.

    A link is made when the normalised title matches exactly, or when exactly one
    game's title is the ranked title plus extra words ("Shantae: Half-Genie Hero"
    -> "... Ultimate Edition"). Anything ambiguous is left for suggest_links.
    Returns the rows that were newly linked.
    """
    games = list(session.scalars(select(Game)))
    by_title: dict[str, list[Game]] = {}
    for game in games:
        by_title.setdefault(_norm(game.name), []).append(game)

    linked = []
    for row in rows:
        if row.app_id is not None:
            continue
        ranked = _norm(row.name)
        matches = by_title.get(ranked, [])
        if not matches:
            matches = [g for g in games if _extends(_norm(g.name), ranked)]
        if len(matches) == 1:
            row.app_id = matches[0].steam_app_id
            linked.append(row)
    return linked


def suggest_links(session: Session, rows: list[RankedGame]) -> dict[int, list[Game]]:
    """For still-unlinked ranked games, the library/family games with similar titles.

    Keyed by row index. These are suggestions only: edit steam_app_id in the CSV
    to accept one.
    """
    games = list(session.scalars(select(Game)))
    suggestions: dict[int, list[Game]] = {}
    for i, row in enumerate(rows):
        if row.app_id is not None:
            continue
        ranked = _norm(row.name)
        found = [g for g in games if _extends(_norm(g.name), ranked)]
        if found:
            suggestions[i] = found[:6]
    return suggestions
