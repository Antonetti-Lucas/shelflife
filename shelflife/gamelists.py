"""Lists of games you make and name: "Best of 2024", "Cozy games", "Souls-likes, ranked".

A list is ranked (numbered, so its order is a ranking) or not (its order is just how you arranged
it), and shows your star ratings under each game or not. Any game ShelfLife knows can be on it,
owned or not. Seeing games side by side in order helps you give each the right stars, but a list
itself never changes your taste: only the stars do, as everywhere else.
"""

from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from shelflife.models import Game, GameList, GameListEntry, ListFolder

NAME_MAX = 80
DESCRIPTION_MAX = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean(name: str, description: str | None) -> tuple[str, str | None]:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("A list needs a name.")
    if len(name) > NAME_MAX:
        raise ValueError(f"A list's name can be at most {NAME_MAX} characters.")
    description = (description or "").strip()
    if len(description) > DESCRIPTION_MAX:
        raise ValueError(f"A description can be at most {DESCRIPTION_MAX} characters.")
    return name, description or None


def all_lists(session: Session) -> list[GameList]:
    """Your lists, the most recently changed first."""
    return list(session.scalars(select(GameList).order_by(GameList.updated_at.desc(), GameList.id.desc())))


# ---- folders ----


def _folder_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("A folder needs a name.")
    if len(name) > NAME_MAX:
        raise ValueError(f"A folder's name can be at most {NAME_MAX} characters.")
    return name


def _check_folder(session: Session, folder_id: int | None) -> None:
    if folder_id is not None and session.get(ListFolder, folder_id) is None:
        raise LookupError("No such folder.")


def all_folders(session: Session) -> list[ListFolder]:
    """Your folders, by name."""
    return list(session.scalars(select(ListFolder).order_by(func.lower(ListFolder.name))))


def get_folder(session: Session, folder_id: int) -> ListFolder:
    found = session.get(ListFolder, folder_id)
    if found is None:
        raise LookupError("No such folder.")
    return found


def create_folder(session: Session, name: str) -> ListFolder:
    made = ListFolder(name=_folder_name(name), created_at=_now())
    session.add(made)
    session.commit()
    return made


def rename_folder(session: Session, folder_id: int, name: str) -> None:
    get_folder(session, folder_id).name = _folder_name(name)
    session.commit()


def delete_folder(session: Session, folder_id: int) -> None:
    """Delete a folder; its lists stay, out of any folder."""
    folder = get_folder(session, folder_id)
    for gl in session.scalars(select(GameList).where(GameList.folder_id == folder_id)):
        gl.folder_id = None
    session.delete(folder)
    session.commit()


def get_list(session: Session, list_id: int) -> GameList:
    found = session.get(GameList, list_id)
    if found is None:
        raise LookupError("No such list.")
    return found


def create_list(
    session: Session,
    name: str,
    description: str | None = None,
    ranked: bool = False,
    show_ratings: bool = True,
    folder_id: int | None = None,
) -> GameList:
    name, description = _clean(name, description)
    _check_folder(session, folder_id)
    now = _now()
    made = GameList(
        name=name, description=description, ranked=ranked, show_ratings=show_ratings, folder_id=folder_id,
        created_at=now, updated_at=now,
    )
    session.add(made)
    session.commit()
    return made


def update_list(
    session: Session,
    list_id: int,
    name: str,
    description: str | None,
    ranked: bool,
    show_ratings: bool,
    folder_id: int | None = None,
) -> GameList:
    found = get_list(session, list_id)
    found.name, found.description = _clean(name, description)
    _check_folder(session, folder_id)
    found.ranked, found.show_ratings, found.folder_id = ranked, show_ratings, folder_id
    found.updated_at = _now()
    session.commit()
    return found


def set_option(session: Session, list_id: int, option: str, value: bool) -> GameList:
    """Turn "ranked" or "show_ratings" on or off."""
    if option not in ("ranked", "show_ratings"):
        raise ValueError(f"Unknown list option: {option}")
    found = get_list(session, list_id)
    setattr(found, option, value)
    found.updated_at = _now()
    session.commit()
    return found


def delete_list(session: Session, list_id: int) -> None:
    found = get_list(session, list_id)
    session.execute(delete(GameListEntry).where(GameListEntry.list_id == list_id))
    session.delete(found)
    session.commit()


def entries(session: Session, list_id: int) -> list[tuple[GameListEntry, Game]]:
    """The list's games, in its order."""
    return list(
        session.execute(
            select(GameListEntry, Game)
            .join(Game, Game.steam_app_id == GameListEntry.steam_app_id)
            .where(GameListEntry.list_id == list_id)
            .order_by(GameListEntry.position)
        ).all()
    )


def counts(session: Session) -> dict[int, int]:
    """list id -> how many games it has."""
    rows = session.execute(select(GameListEntry.list_id, func.count()).group_by(GameListEntry.list_id))
    return {list_id: n for list_id, n in rows}


def lists_with(session: Session, app_id: int) -> list[GameList]:
    """Your lists that have this game, by name."""
    return list(
        session.scalars(
            select(GameList)
            .join(GameListEntry, GameListEntry.list_id == GameList.id)
            .where(GameListEntry.steam_app_id == app_id)
            .order_by(func.lower(GameList.name))
        )
    )


def _touch(session: Session, list_id: int) -> None:
    get_list(session, list_id).updated_at = _now()


def _renumber(rows: list[GameListEntry]) -> None:
    for i, row in enumerate(rows, start=1):
        row.position = i


def _rows(session: Session, list_id: int) -> list[GameListEntry]:
    return list(
        session.scalars(select(GameListEntry).where(GameListEntry.list_id == list_id).order_by(GameListEntry.position))
    )


def add_game(session: Session, list_id: int, app_id: int) -> bool:
    """Put a game at the end of the list. False if it was already on it."""
    get_list(session, list_id)
    if session.get(Game, app_id) is None:
        raise LookupError("No such game.")
    if session.get(GameListEntry, (list_id, app_id)) is not None:
        return False
    last = session.scalar(select(func.max(GameListEntry.position)).where(GameListEntry.list_id == list_id)) or 0
    session.add(GameListEntry(list_id=list_id, steam_app_id=app_id, position=last + 1, added_at=_now()))
    _touch(session, list_id)
    session.commit()
    return True


def remove_game(session: Session, list_id: int, app_id: int) -> None:
    """Take a game off the list; the ones after it move up."""
    rows = _rows(session, list_id)
    gone = next((r for r in rows if r.steam_app_id == app_id), None)
    if gone is None:
        return
    session.delete(gone)
    _renumber([r for r in rows if r is not gone])
    _touch(session, list_id)
    session.commit()


def move_game(session: Session, list_id: int, app_id: int, to: int) -> None:
    """Move a game to place `to` (1 is the first; out of range means the first or the last)."""
    rows = _rows(session, list_id)
    moving = next((r for r in rows if r.steam_app_id == app_id), None)
    if moving is None:
        raise LookupError("That game isn't on this list.")
    rows.remove(moving)
    rows.insert(min(max(to, 1), len(rows) + 1) - 1, moving)
    _renumber(rows)
    _touch(session, list_id)
    session.commit()


def set_order(session: Session, list_id: int, app_ids: list[int]) -> None:
    """Put the list in this order (after dragging games around). It must name each of its games once."""
    rows = {r.steam_app_id: r for r in _rows(session, list_id)}
    if sorted(app_ids) != sorted(rows):
        raise ValueError("The new order must have each of the list's games once; reload the page and try again.")
    _renumber([rows[a] for a in app_ids])
    _touch(session, list_id)
    session.commit()


def addable(session: Session, list_id: int, query: str, limit: int = 24) -> list[Game]:
    """Games whose name has every word of `query`, to add to the list: not already on it, the games
    in your library first, each part by name."""
    words = query.lower().split()
    if not words:
        return []
    on_list = set(session.scalars(select(GameListEntry.steam_app_id).where(GameListEntry.list_id == list_id)))
    found = select(Game).where(or_(Game.type == "game", Game.type.is_(None)))
    for word in words:
        found = found.where(func.lower(Game.name).contains(word))
    games = [g for g in session.scalars(found) if g.steam_app_id not in on_list]
    games.sort(key=lambda g: (g.library_entry is None, g.name.lower()))
    return games[:limit]
