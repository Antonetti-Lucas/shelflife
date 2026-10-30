"""Import the Steam Family Sharing library exported by tools/export_family_library.js."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.models import Game, LibraryEntry

EXPORT_PATH = Path("data/family-library.json")
EXPORT_FORMAT = 1


@dataclass(frozen=True)
class FamilyApp:
    app_id: int
    owner_steamids: tuple[str, ...]
    exclude_reason: int  # 0 = playable by you
    name: str | None


@dataclass(frozen=True)
class ImportResult:
    added: int  # new family games
    already_known: int  # family games that were already imported
    pruned: int  # previously imported, no longer shared with you


def load_export(path: Path = EXPORT_PATH) -> tuple[str | None, list[FamilyApp]]:
    """Return (owner steamid of the export, its apps), validating the file shape."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("format") != EXPORT_FORMAT or not isinstance(data.get("apps"), list):
        raise ValueError(
            f"{path} is not a ShelfLife family export (expected format {EXPORT_FORMAT}). "
            "Create it with tools/export_family_library.js."
        )
    apps = [
        FamilyApp(
            app_id=int(a["appid"]),
            owner_steamids=tuple(str(s) for s in a.get("owner_steamids", [])),
            exclude_reason=int(a.get("exclude_reason", 0)),
            name=a.get("name"),
        )
        for a in data["apps"]
    ]
    return data.get("owner_steamid"), apps


def shared_with_you(apps: list[FamilyApp], steam_id: str) -> list[FamilyApp]:
    """Apps you can play through the family but don't own yourself."""
    return [a for a in apps if steam_id not in a.owner_steamids and a.exclude_reason == 0]


def import_family(
    session: Session,
    shared: list[FamilyApp],
    names: dict[int, str],
) -> ImportResult:
    """Create Game + LibraryEntry rows for family games; drop ones no longer shared.

    Family games get a library entry with zero playtime, because Steam's Web API
    doesn't report playtime for games you don't own. Games you own are left alone.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    wanted = {a.app_id for a in shared}

    added = already_known = 0
    for app in shared:
        game = session.get(Game, app.app_id)
        entry = session.get(LibraryEntry, app.app_id)
        if entry is not None and not game.family_shared:
            continue  # an owned game: the normal sync is the source of truth
        if game is None:
            name = app.name or names.get(app.app_id) or f"App {app.app_id}"
            game = Game(steam_app_id=app.app_id, name=name)
            session.add(game)
        if entry is not None:
            already_known += 1
            continue
        game.family_shared = True
        session.add(
            LibraryEntry(
                steam_app_id=app.app_id,
                playtime_total_min=0,
                playtime_recent_min=0,
                last_synced=now,
            )
        )
        added += 1

    pruned = 0
    stale = session.scalars(
        select(Game).where(Game.family_shared.is_(True), Game.steam_app_id.notin_(wanted))
    )
    for game in stale:
        entry = session.get(LibraryEntry, game.steam_app_id)
        if entry is not None:
            session.delete(entry)
        game.family_shared = False
        pruned += 1

    session.commit()
    return ImportResult(added=added, already_known=already_known, pruned=pruned)
