"""Helpers that build small, readable test libraries."""

from datetime import datetime

from shelflife.models import Game, LibraryEntry

NOW = datetime(2026, 6, 1)


def add_game(
    session,
    app_id,
    name=None,
    *,
    tags=None,
    owned=True,
    family=False,
    minutes=0,
    recent=0,
    last_played=None,
    achievements=None,  # (unlocked, total)
    unlock_times=None,
    rarest=None,
    reviews=None,  # (positive, negative)
    type="game",
):
    """Add a game. owned=True: in your library; family=True: shared with you;
    neither: a game you don't own (the discovery pool)."""
    game = Game(
        steam_app_id=app_id,
        name=name or f"Game {app_id}",
        type=type,
        tags=tags or {},
        genres=[],
        family_shared=True if family else (False if owned else None),
    )
    if reviews:
        game.reviews_positive, game.reviews_negative = reviews
        game.reviews_fetched_at = NOW
    session.add(game)
    if owned or family:
        entry = LibraryEntry(
            steam_app_id=app_id,
            playtime_total_min=minutes,
            playtime_recent_min=recent,
            last_played=last_played,
            last_synced=NOW,
            rarest_unlocked_pct=rarest,
            unlock_times=unlock_times,
        )
        if achievements:
            entry.achievements_unlocked, entry.achievements_total = achievements
        session.add(entry)
    session.commit()
    return game


def build_scenario(session):
    """A small library with a clear taste (stealth liked, sports disliked) and one
    candidate of every kind. Returns the ratings that define the taste."""
    add_game(session, 1, "Stealth Hero", tags={"Stealth": 100, "Action": 50}, minutes=3000)
    add_game(session, 2, "Sports Pro", tags={"Sports": 100, "Multiplayer": 40}, minutes=3000)
    # Fillers, so that tag rarity (idf) is meaningful: played, but not engaging enough
    # to shape the taste.
    for i, tag in enumerate(["Puzzle", "Racing", "Farming", "Chess", "Platformer", "Horror"], start=10):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)

    add_game(session, 20, "Backlog Stealth", tags={"Stealth": 100, "Action": 30})  # backlog
    add_game(session, 21, "Backlog Sports", tags={"Sports": 100})  # backlog
    add_game(session, 30, "Family Stealth", tags={"Stealth": 100}, owned=False, family=True)
    add_game(session, 40, "Pool Stealth Good", tags={"Stealth": 100, "Action": 30}, owned=False, reviews=(9000, 500))
    add_game(session, 41, "Pool Stealth Bad", tags={"Stealth": 100, "Action": 30}, owned=False, reviews=(300, 300))
    # An edition of a game you already played: must not be recommended as new.
    add_game(session, 42, "Stealth Hero Remastered", tags={"Stealth": 100}, owned=False, reviews=(9000, 500))
    return {1: 5.0, 2: 1.0}
