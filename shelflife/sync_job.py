"""Sync from the web app: the same steps as the command-line scripts, in the background.

1. your library and playtime (one call to Steam's API);
2. your wishlist (a game id, your rank and the date you added it; a wishlist Steam cannot
   give does not stop the rest);
3. your achievements (official API, quick);
4. store details and tags for new games and wishlist games (slow: about 3 seconds a game, so
   the very first sync takes a while), plus games that came back empty last time and a few of
   the oldest;
5. the store's "About this game" text for games that have their details but not the text yet
   (a one-time catch-up for games read before the text was kept; new games get it in step 4);
6. review counts for every game that has none or whose counts are a month old;
7. how long other players play each game you have played (from up to 500 of its reviews), the
   yardstick your own hours are measured against; read once, then every six months.

Each step re-reads only what could have changed (see the rules in shelflife/sync.py);
"check everything again" re-reads it all. Every step saves game by game, so a sync can be
stopped, or fail, and the next one carries on where it left off. It only runs when you press
the button.
"""

from dataclasses import dataclass

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shelflife.db import SessionLocal
from shelflife.jobs import BackgroundJob, JobSnapshot
from shelflife.models import Game, LibraryEntry, WishlistItem
from shelflife.steam import SteamError, fetch_owned_games
from shelflife.sync import (
    about_due,
    achievements_due,
    details_due,
    enrich_games,
    reviews_due,
    sync_about,
    player_hours_due,
    sync_achievements,
    sync_all_reviews,
    sync_player_hours,
    upsert_library,
)
from shelflife.throttle import RateLimited
from shelflife.wishlist import fetch_wishlist, import_wishlist

STEPS = (
    ("library", "Reading your library"),
    ("wishlist", "Reading your wishlist"),
    ("achievements", "Reading your achievements"),
    ("details", "Fetching store details for new games"),
    ("about", "Reading game descriptions"),
    ("reviews", "Counting reviews"),
    ("players", "Reading how long other players play"),
)
SECONDS_PER_GAME = 3  # store details and tags, for the time estimate
SECONDS_PER_ACHIEVEMENT = 1  # a game's achievements: two quick calls
SECONDS_PER_REVIEW = 1.2
SECONDS_PER_ABOUT = 1.6  # one store call for a game's description
SECONDS_PER_PLAYERS = 5.5  # up to five pages of reviews


@dataclass
class SyncResult:
    games: int = 0  # games you own, as Steam reports it (Family Sharing games are not in it)
    new_games: int = 0  # of those, how many were not in the database before
    wishlist: int = 0  # games on your wishlist
    wishlist_added: int = 0  # of those, how many are new since the last sync
    wishlist_note: str | None = None  # why the wishlist could not be updated (private, or Steam failed)
    achievements: int = 0  # games whose achievements were fetched
    details: int = 0  # games whose store details and tags were fetched
    about: int = 0  # games whose description text was read
    reviews: int = 0  # backlog games whose review counts were fetched
    players: int = 0  # games whose players' hours were read
    full: bool = False  # a "check everything again" run, not the usual sync
    stopped: bool = False  # stopped from the page
    aborted: str | None = None  # why it gave up early (rate limit, network, Steam)


def run_sync(
    session: Session, api_key: str, steam_id: str, on_progress=None, should_stop=None, full: bool = False
) -> SyncResult:
    """Run the seven steps. on_progress(step, text, done, total): `step` counts from 1.
    `full` re-reads everything (all achievements, all store details, all review counts)
    instead of only what is due.

    Ends early, keeping everything saved so far, when should_stop() says so or when Steam
    or SteamSpy turns it away.
    """
    report = on_progress or (lambda *args: None)
    stop = should_stop or (lambda: False)
    result = SyncResult(full=full)
    before = set(session.scalars(select(Game.steam_app_id)))
    try:
        report(1, "Reading your library from Steam", 0, 1)
        owned = fetch_owned_games(api_key, steam_id)
        upsert_library(session, owned)
        result.games = len(owned)
        result.new_games = len({o.app_id for o in owned} - before)
        report(1, "Library saved", 1, 1)
        if stop():
            result.stopped = True
            return result

        report(2, "Reading your wishlist from Steam", 0, 1)
        try:  # a wishlist Steam cannot give us must not stop the rest of the sync
            imported = import_wishlist(session, fetch_wishlist(steam_id))
            result.wishlist, result.wishlist_added = imported.total, imported.added
            if imported.kept_old_list:
                result.wishlist_note = "Steam returned an empty wishlist (is it private?), so your earlier one was kept"
        except RateLimited as exc:
            result.wishlist_note = f"it could not be read: {exc}"
        except (requests.RequestException, ValueError) as exc:
            session.rollback()
            result.wishlist_note = f"it could not be read ({type(exc).__name__})"
        report(2, "Wishlist saved", 1, 1)
        if stop():
            result.stopped = True
            return result

        result.achievements = sync_achievements(
            session,
            api_key,
            steam_id,
            on_progress=lambda i, n, game, stats: report(3, f"Achievements for {game.name}", i, n),
            should_stop=stop,
            everything=full,
        )
        if stop():
            result.stopped = True
            return result

        result.details = enrich_games(
            session,
            on_progress=lambda i, n, game: report(4, f"Store details for {game.name}", i, n),
            should_stop=stop,
            refresh=True,
            everything=full,
        )
        if stop():
            result.stopped = True
            return result

        result.about = sync_about(
            session,
            on_progress=lambda i, n, game: report(5, f"Description of {game.name}", i, n),
            should_stop=stop,
        )
        if stop():
            result.stopped = True
            return result

        result.reviews = sync_all_reviews(
            session,
            on_progress=lambda i, n, game: report(6, f"Review counts for {game.name}", i, n),
            should_stop=stop,
            everything=full,
        )
        if stop():
            result.stopped = True
            return result

        result.players = sync_player_hours(
            session,
            on_progress=lambda i, n, game: report(7, f"How long players play {game.name}", i, n),
            should_stop=stop,
            everything=full,
        )
        result.stopped = stop()
    except RateLimited as exc:
        result.aborted = str(exc)
    except SteamError as exc:
        result.aborted = str(exc)
    except requests.RequestException as exc:
        result.aborted = f"Network error: {exc}"
    return result


def summarize_sync(result: SyncResult) -> JobSnapshot:
    """Turn a finished sync into the message the page shows."""
    parts = [f"{result.games} owned game{'' if result.games == 1 else 's'}"]
    if result.new_games:
        parts[0] += f" ({result.new_games} new)"
    if result.wishlist:
        parts.append(f"{result.wishlist} on your wishlist" + (f" ({result.wishlist_added} new)" if result.wishlist_added else ""))
    if result.wishlist_note:
        parts.append(f"your wishlist: {result.wishlist_note}")
    if result.achievements:
        parts.append(f"achievements for {result.achievements} games")
    if result.details:
        parts.append(f"store details for {result.details} games")
    if result.about:
        parts.append(f"descriptions for {result.about} games")
    if result.reviews:
        parts.append(f"review counts for {result.reviews} games")
    if result.players:
        parts.append(f"how long players play {result.players} games")
    base = ("Checked everything again: " if result.full else "Synced: ") + "; ".join(parts) + "."
    if result.aborted:
        return JobSnapshot(
            state="error",
            message=f"Stopped early: {result.aborted} Progress is saved; sync again later to carry on. " + base,
        )
    if result.stopped:
        return JobSnapshot(state="stopped", message="Stopped. " + base + " Sync again to carry on where it stopped.")
    return JobSnapshot(state="done", message=base)


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60)) if seconds else 0


def what_is_due(session: Session, with_due: bool = True) -> dict[str, int | object]:
    """What the next sync would do and roughly how long it takes, and what a full re-check
    would do: library size (owned and from Family Sharing), the games due for each step, and
    the last sync. The page and the sync use the same rules, so they agree.

    Working out what is due reads every game, so `with_due=False` skips it (the page does
    while a sync runs and refreshes itself every few seconds); the due numbers are then None."""
    library = session.scalar(select(func.count()).select_from(LibraryEntry)) or 0
    family = session.scalar(
        select(func.count())
        .select_from(Game)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(Game.family_shared.is_(True))
    )
    counts = {"achievements": None, "details": None, "about": None, "reviews": None, "players": None}
    minutes = full_minutes = None
    if with_due:
        counts = {
            "achievements": len(achievements_due(session)),
            "details": len(details_due(session)),
            "about": len(about_due(session)),
            "reviews": len(reviews_due(session)),
            "players": len(player_hours_due(session)),
        }
        full = {
            "achievements": len(achievements_due(session, everything=True)),
            "details": len(details_due(session, everything=True)),
            "about": 0,  # a full re-read of the details brings the text with it
            "reviews": len(reviews_due(session, everything=True)),
            "players": len(player_hours_due(session, everything=True)),
        }
        estimate = lambda c: _minutes(  # noqa: E731
            c["achievements"] * SECONDS_PER_ACHIEVEMENT
            + c["details"] * SECONDS_PER_GAME
            + c["about"] * SECONDS_PER_ABOUT
            + c["reviews"] * SECONDS_PER_REVIEW
            + c["players"] * SECONDS_PER_PLAYERS
        )
        minutes, full_minutes = estimate(counts), estimate(full)
    return {
        "library": library,
        "wishlist": session.scalar(select(func.count()).select_from(WishlistItem)) or 0,
        "family": family or 0,
        "owned": library - (family or 0),
        **counts,
        "minutes": minutes,
        "full_minutes": full_minutes,
        "last_synced": session.scalar(select(func.max(LibraryEntry.last_synced))),
    }


class SyncJob(BackgroundJob):
    """The sync, run in the background."""

    def start(self, api_key: str, steam_id: str, full: bool = False) -> bool:
        """Start a sync (`full`: re-read everything); False if one is already running."""
        started = self._begin(
            JobSnapshot(state="running", phase="library", text="Starting", step=1, steps=len(STEPS))
        )
        if not started:
            return False
        self._spawn(lambda: self._work(api_key, steam_id, full))
        return True

    def _work(self, api_key: str, steam_id: str, full: bool = False) -> None:
        def progress(step: int, text: str, done: int, total: int) -> None:
            self._set(step=step, phase=STEPS[step - 1][0], text=text, done=done, total=total)

        try:
            with SessionLocal() as session:
                result = run_sync(
                    session, api_key, steam_id, on_progress=progress, should_stop=self._stop.is_set, full=full
                )
            snapshot = summarize_sync(result)
        except Exception as exc:  # never leave the page stuck on "running"
            reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
            snapshot = JobSnapshot(state="error", message=f"Stopped early: {reason}")
        self._end(snapshot)


job = SyncJob()
