"""A game's series and first release date, from what Wikidata said (Game.wikidata, see shelflife/wikidata.py).

Wikidata knows every game of a series, ShelfLife only the ones it has stored: a series is shown when two
or more of its games are in the database, in the series' own order (the number in the series if every
game has one, else by first release date).
"""

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shelflife.models import Game

MAX_SERIES = 2  # a game can be in several (a game and a franchise); show the most specific few

_index_cache: dict = {"key": None, "members": {}}


def first_year(game: Game) -> int | None:
    """The year of the game's first release, per Wikidata."""
    released = (game.wikidata or {}).get("released") or ""
    return int(released[:4]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", released) else None


@dataclass(frozen=True)
class SeriesMember:
    game: Game
    ordinal: str | None
    year: int | None


@dataclass(frozen=True)
class Series:
    id: str
    label: str
    members: list[SeriesMember]  # in the series' order, including the game asked about


def _members_by_series(session: Session) -> dict[str, list[tuple[int, str | None]]]:
    """Series id -> [(game id, its number in the series)], for every game stored; remembered until a
    game's Wikidata answer changes."""
    key = tuple(session.execute(select(func.count(), func.max(Game.wikidata_fetched_at)).where(Game.wikidata.is_not(None))).one())
    if _index_cache["key"] != key:
        members: dict[str, list[tuple[int, str | None]]] = {}
        for app_id, data in session.execute(select(Game.steam_app_id, Game.wikidata).where(Game.wikidata.is_not(None), Game.type == "game")):
            for s in (data or {}).get("series") or []:
                members.setdefault(s["id"], []).append((app_id, s.get("ordinal")))
        _index_cache.update(key=key, members=members)
    return _index_cache["members"]


def _number(ordinal: str | None) -> float | None:
    found = re.match(r"\d+(?:\.\d+)?", (ordinal or "").strip())
    return float(found.group()) if found else None


def series_of(session: Session, game: Game) -> list[Series]:
    """The series this game belongs to with two or more games stored, the smallest (most specific) first."""
    mine = (game.wikidata or {}).get("series") or []
    index = _members_by_series(session)
    found = []
    for s in mine:
        entries = index.get(s["id"], [])
        if len(entries) < 2:
            continue
        games = {g.steam_app_id: g for g in session.scalars(select(Game).where(Game.steam_app_id.in_([a for a, _ in entries])))}
        members = [SeriesMember(games[a], o, first_year(games[a])) for a, o in entries if a in games]
        if len(members) < 2:
            continue
        numbered = all(_number(m.ordinal) is not None for m in members)
        members.sort(
            key=(lambda m: (_number(m.ordinal), m.year or 9999, m.game.name.lower()))
            if numbered
            else (lambda m: (m.year or 9999, m.game.name.lower()))
        )
        found.append(Series(s["id"], s["label"], members))
    found.sort(key=lambda s: (len(s.members), s.label))
    return found[:MAX_SERIES]
