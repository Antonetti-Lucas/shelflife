"""Runs "find more games" in the background so the web page does not wait for it.

Fetching is slow on purpose (about 3 seconds a game, to stay polite to Steam and
SteamSpy), so the button starts a thread and the page shows its progress. It only
runs when you press the button.
"""

from shelflife.db import SessionLocal
from shelflife.discovery import FindResult, find_games
from shelflife.jobs import BackgroundJob, JobSnapshot  # noqa: F401  (JobSnapshot is re-exported)
from shelflife.models import Game
from shelflife.similar import reference_vector, tag_idf

SECONDS_PER_GAME = 5  # for the time estimate shown next to the button; narrow tags take longer


def summarize(
    result: FindResult, tags: list[str], like: tuple[int, str] | None = None, popular: bool = False
) -> JobSnapshot:
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
    if result.not_games:
        parts.append(f"{result.not_games} turned out not to be games (DLC or software) and are hidden")
    if result.failed:
        parts.append(f"{result.failed} could not be fetched and will be retried next time")
    if result.empty_tags:
        names = ", ".join(f"“{t}”" for t in result.empty_tags)
        parts.append(f"SteamSpy lists no games for {names} (it does not know that tag name; a related tag may work)")
    message = "; ".join(parts) + "."
    if result.aborted:
        message = f"Stopped early: {result.aborted} Progress is saved; try again later. " + message
        return JobSnapshot(state="error", message=message, like_id=like_id, like_name=like_name, popular=popular)
    if result.stopped:
        return JobSnapshot(
            state="stopped", message="Stopped. " + message, like_id=like_id, like_name=like_name, popular=popular
        )
    return JobSnapshot(
        state="done",
        message=message + " They are ranked on this page.",
        like_id=like_id,
        like_name=like_name,
        popular=popular,
    )


class DiscoverJob(BackgroundJob):
    """The search for new games."""

    def start(
        self,
        tags: list[str],
        count: int,
        min_reviews: int,
        like: tuple[int, str] | None = None,
        popular: bool = False,
    ) -> bool:
        """Start a search; False if one is already running. `like`: (app id, name) of a
        game to find close matches for. `popular`: search SteamSpy's popular games, not tags."""
        started = self._begin(
            JobSnapshot(
                state="running",
                phase="lists",
                text="Starting",
                total=len(tags),
                like_id=like[0] if like else None,
                like_name=like[1] if like else "",
                popular=popular,
            )
        )
        if not started:
            return False
        self._spawn(lambda: self._work(tags, count, min_reviews, like, popular))
        return True

    def _work(
        self,
        tags: list[str],
        count: int,
        min_reviews: int,
        like: tuple[int, str] | None = None,
        popular: bool = False,
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
                    popular=popular,
                )
            snapshot = summarize(result, tags, like, popular)
        except Exception as exc:  # never leave the page stuck on "running"
            reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__  # one line, not a page of SQL
            snapshot = JobSnapshot(
                state="error",
                message=f"Stopped early: {reason}",
                like_id=like[0] if like else None,
                like_name=like[1] if like else "",
                popular=popular,
            )
        self._end(snapshot)


job = DiscoverJob()
