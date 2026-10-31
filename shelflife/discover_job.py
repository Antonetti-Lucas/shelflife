"""Runs "find more games" in the background so the web page does not wait for it.

Fetching is slow on purpose (about 3 seconds a game, to stay polite to Steam and
SteamSpy), so the button starts a thread and the page shows its progress. Only one
search runs at a time, and it only runs when you press the button.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from shelflife.db import SessionLocal
from shelflife.discovery import FindResult, find_games
from shelflife.models import Game
from shelflife.similar import reference_vector, tag_idf

SECONDS_PER_GAME = 5  # for the time estimate shown next to the button; narrow tags take longer


@dataclass(frozen=True)
class JobSnapshot:
    """What the page shows. state: idle, running, done, stopped or error."""

    state: str = "idle"
    phase: str = ""
    text: str = ""
    done: int = 0
    total: int = 0
    message: str = ""  # the outcome of the last search, or why it could not start
    like_id: int | None = None  # the game a "games like this" search was for
    like_name: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def failed(self) -> bool:
        return self.state == "error"


def summarize(result: FindResult, tags: list[str], like: tuple[int, str] | None = None) -> JobSnapshot:
    """Turn a finished search into the message the page shows."""
    like_id, like_name = like if like else (None, "")
    parts = []
    if result.added or result.enriched:
        parts.append(f"Added {result.added} new game{'' if result.added == 1 else 's'}")
        if result.enriched != result.added:
            parts.append(f"fetched details for {result.enriched}")
    elif not result.aborted:
        parts.append("No new games found")
    if result.skipped:
        why = f"that are not close enough to {like_name}" if like else "that only mention the tag in passing"
        parts.append(f"looked at {result.checked} and set aside {result.skipped} {why}")
    if result.failed:
        parts.append(f"{result.failed} could not be fetched and will be retried next time")
    if result.empty_tags:
        names = ", ".join(f"“{t}”" for t in result.empty_tags)
        parts.append(f"SteamSpy lists no games for {names} (check the spelling)")
    message = "; ".join(parts) + "."
    if result.aborted:
        message = f"Stopped early: {result.aborted} Progress is saved; try again later. " + message
        return JobSnapshot(state="error", message=message, like_id=like_id, like_name=like_name)
    if result.stopped:
        return JobSnapshot(state="stopped", message="Stopped. " + message, like_id=like_id, like_name=like_name)
    return JobSnapshot(
        state="done", message=message + " They are ranked on this page.", like_id=like_id, like_name=like_name
    )


class DiscoverJob:
    def __init__(self, spawn: Callable[[Callable[[], None]], None] | None = None):
        self._lock = threading.Lock()
        self._snapshot = JobSnapshot()
        self._stop = threading.Event()
        # How the work gets started; tests pass something that runs it right away.
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def snapshot(self) -> JobSnapshot:
        with self._lock:
            return self._snapshot

    def _set(self, **changes) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def fail(self, message: str) -> None:
        """Show why a search could not start (unless one is running)."""
        with self._lock:
            if not self._snapshot.running:
                self._snapshot = JobSnapshot(state="error", message=message)

    def start(
        self, tags: list[str], count: int, min_reviews: int, like: tuple[int, str] | None = None
    ) -> bool:
        """Start a search; False if one is already running. `like`: (app id, name) of a
        game to find close matches for."""
        with self._lock:
            if self._snapshot.running:
                return False
            self._stop.clear()
            self._snapshot = JobSnapshot(
                state="running",
                phase="lists",
                text="Starting",
                total=len(tags),
                like_id=like[0] if like else None,
                like_name=like[1] if like else "",
            )
        self._spawn(lambda: self._work(tags, count, min_reviews, like))
        return True

    def cancel(self) -> None:
        """Ask a running search to stop after the game it is on."""
        self._stop.set()

    def _work(
        self, tags: list[str], count: int, min_reviews: int, like: tuple[int, str] | None = None
    ) -> None:
        def progress(phase: str, done: int, total: int, text: str) -> None:
            self._set(phase=phase, done=done, total=total, text=text)

        try:
            with SessionLocal() as session:
                reference = None
                if like:
                    idf = tag_idf(session)
                    reference = (reference_vector(session.get(Game, like[0]), idf), idf)
                result = find_games(
                    session,
                    tags,
                    count,
                    min_reviews,
                    on_progress=progress,
                    should_stop=self._stop.is_set,
                    like=reference,
                )
            snapshot = summarize(result, tags, like)
        except Exception as exc:  # never leave the page stuck on "running"
            snapshot = JobSnapshot(
                state="error",
                message=f"Stopped early: {exc}",
                like_id=like[0] if like else None,
                like_name=like[1] if like else "",
            )
        with self._lock:
            self._snapshot = snapshot


job = DiscoverJob()
