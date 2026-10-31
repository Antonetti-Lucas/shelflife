"""Sync from the web app: the same four steps as the command-line scripts, in the background.

1. your library and playtime (one call to Steam's API);
2. your achievements (official API, quick);
3. store details and tags for games that have none yet (slow: about 3 seconds a game, so
   the very first sync takes a while, later ones only handle new games);
4. review counts for the games you own but have barely played.

Every step saves game by game, so a sync can be stopped, or fail, and the next one
carries on where it left off. It only runs when you press the button.
"""

from dataclasses import dataclass

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shelflife.db import SessionLocal
from shelflife.jobs import BackgroundJob, JobSnapshot
from shelflife.models import Game, LibraryEntry
from shelflife.recommend import BACKLOG_MAX_MIN
from shelflife.steam import SteamError, fetch_owned_games
from shelflife.sync import enrich_games, sync_achievements, sync_backlog_reviews, upsert_library
from shelflife.throttle import RateLimited

STEPS = (
    ("library", "Reading your library"),
    ("achievements", "Reading your achievements"),
    ("details", "Fetching store details for new games"),
    ("reviews", "Counting reviews for your backlog"),
)
SECONDS_PER_GAME = 3  # store details and tags, for the time estimate


@dataclass
class SyncResult:
    games: int = 0  # games you own, as Steam reports it (Family Sharing games are not in it)
    new_games: int = 0  # of those, how many were not in the database before
    achievements: int = 0  # games whose achievements were fetched
    details: int = 0  # games whose store details and tags were fetched
    reviews: int = 0  # backlog games whose review counts were fetched
    stopped: bool = False  # stopped from the page
    aborted: str | None = None  # why it gave up early (rate limit, network, Steam)


def run_sync(session: Session, api_key: str, steam_id: str, on_progress=None, should_stop=None) -> SyncResult:
    """Run the four steps. on_progress(step, text, done, total): `step` counts from 1.

    Ends early, keeping everything saved so far, when should_stop() says so or when Steam
    or SteamSpy turns it away.
    """
    report = on_progress or (lambda *args: None)
    stop = should_stop or (lambda: False)
    result = SyncResult()
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

        result.achievements = sync_achievements(
            session,
            api_key,
            steam_id,
            on_progress=lambda i, n, game, stats: report(2, f"Achievements for {game.name}", i, n),
            should_stop=stop,
        )
        if stop():
            result.stopped = True
            return result

        result.details = enrich_games(
            session,
            on_progress=lambda i, n, game: report(3, f"Store details for {game.name}", i, n),
            should_stop=stop,
        )
        if stop():
            result.stopped = True
            return result

        result.reviews = sync_backlog_reviews(
            session,
            BACKLOG_MAX_MIN,
            on_progress=lambda i, n, game: report(4, f"Review counts for {game.name}", i, n),
            should_stop=stop,
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
    if result.achievements:
        parts.append(f"achievements for {result.achievements} games")
    if result.details:
        parts.append(f"store details for {result.details} games")
    if result.reviews:
        parts.append(f"review counts for {result.reviews} backlog games")
    base = "Synced: " + "; ".join(parts) + "."
    if result.aborted:
        return JobSnapshot(
            state="error",
            message=f"Stopped early: {result.aborted} Progress is saved; sync again later to carry on. " + base,
        )
    if result.stopped:
        return JobSnapshot(state="stopped", message="Stopped. " + base + " Sync again to carry on where it stopped.")
    return JobSnapshot(state="done", message=base)


def what_is_waiting(session: Session) -> dict[str, int | object]:
    """What a sync still has to do, for the page: library size (owned and from Family
    Sharing), games waiting for store details, played games with no achievements on record,
    and the last sync."""
    library = session.scalar(select(func.count()).select_from(LibraryEntry)) or 0
    family = session.scalar(
        select(func.count())
        .select_from(Game)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(Game.family_shared.is_(True))
    )
    details = session.scalar(
        select(func.count())
        .select_from(Game)
        .join(LibraryEntry, LibraryEntry.steam_app_id == Game.steam_app_id)
        .where(Game.details_fetched_at.is_(None))
    )
    achievements = session.scalar(
        select(func.count())
        .select_from(LibraryEntry)
        .where(LibraryEntry.playtime_total_min > 0, LibraryEntry.achievements_fetched_at.is_(None))
    )
    return {
        "library": library,
        "family": family or 0,
        "owned": library - (family or 0),
        "details": details or 0,
        "achievements": achievements or 0,
        "last_synced": session.scalar(select(func.max(LibraryEntry.last_synced))),
    }


class SyncJob(BackgroundJob):
    """The sync, run in the background."""

    def start(self, api_key: str, steam_id: str) -> bool:
        """Start a sync; False if one is already running."""
        started = self._begin(
            JobSnapshot(state="running", phase="library", text="Starting", step=1, steps=len(STEPS))
        )
        if not started:
            return False
        self._spawn(lambda: self._work(api_key, steam_id))
        return True

    def _work(self, api_key: str, steam_id: str) -> None:
        def progress(step: int, text: str, done: int, total: int) -> None:
            self._set(step=step, phase=STEPS[step - 1][0], text=text, done=done, total=total)

        try:
            with SessionLocal() as session:
                result = run_sync(session, api_key, steam_id, on_progress=progress, should_stop=self._stop.is_set)
            snapshot = summarize_sync(result)
        except Exception as exc:  # never leave the page stuck on "running"
            reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
            snapshot = JobSnapshot(state="error", message=f"Stopped early: {reason}")
        self._end(snapshot)


job = SyncJob()
