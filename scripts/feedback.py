"""Tell ShelfLife which recommendations miss, so it learns from them.

Run from the project root:
    python -m scripts.feedback not-interested "Serial Cleaner"
    python -m scripts.feedback undo "Serial Cleaner"
    python -m scripts.feedback list

(The web app does the same with a button.) A game can be given by name (partial
is fine) or by Steam app id. A game marked not interested stops being recommended
and pushes its tags away in your taste profile (it counts like a 2-star rating).
"""

import argparse
import sys

from sqlalchemy import select

from shelflife import diary
from shelflife.db import init_db, session_scope
from shelflife.models import Game


def _resolve(session, query: str) -> Game | None:
    if query.isdigit():
        game = session.get(Game, int(query))
        if game is None:
            print(f"No game with app id {query} in the database.", file=sys.stderr)
        return game

    matches = list(
        session.scalars(select(Game).where(Game.name.ilike(f"%{query}%")).order_by(Game.name))
    )
    exact = [g for g in matches if g.name.lower() == query.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"No game matching {query!r}.", file=sys.stderr)
    else:
        print(f"{len(matches)} games match {query!r}; use the app id instead:", file=sys.stderr)
        for g in matches[:15]:
            print(f"  {g.steam_app_id:>8}  {g.name}", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("not-interested", "undo"):
        sub.add_parser(name).add_argument("game", help="game name or Steam app id")
    sub.add_parser("list")
    args = parser.parse_args()

    init_db()
    with session_scope() as session:
        if args.command == "list":
            marked = diary.load_not_interested(session)
            print("\n".join(f"{i:>8}  {n}" for i, n in marked.items()) or "Nothing marked yet.")
            return 0

        game = _resolve(session, args.game)
        if game is None:
            return 1
        if args.command == "not-interested":
            diary.set_not_interested(session, game.steam_app_id, True)
            print(f"Marked not interested: {game.name}")
        else:
            diary.set_not_interested(session, game.steam_app_id, False)
            print(f"Removed the mark from: {game.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
