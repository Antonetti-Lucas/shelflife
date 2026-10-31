"""Background tasks for the web app: a thread, a progress snapshot and a stop flag.

Fetching from Steam is slow on purpose (to stay polite to Valve and SteamSpy), so a
button starts a task in a thread and the page shows its progress. A task only runs
when you press its button, one at a time, and it can be stopped part-way.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class JobSnapshot:
    """What a page shows about a task. state: idle, running, done, stopped or error."""

    state: str = "idle"
    phase: str = ""
    text: str = ""
    done: int = 0
    total: int = 0
    message: str = ""  # the outcome of the last run, or why it could not start
    step: int = 0  # for tasks made of steps: which one, out of `steps`
    steps: int = 0
    like_id: int | None = None  # the game a "games like this" search was for
    like_name: str = ""
    popular: bool = False  # a search through SteamSpy's popular games instead of tags

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def failed(self) -> bool:
        return self.state == "error"


class BackgroundJob:
    """One task at a time. Subclasses say what to run; this keeps the state."""

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

    def _begin(self, snapshot: JobSnapshot) -> bool:
        """Claim the task: False if it is already running."""
        with self._lock:
            if self._snapshot.running:
                return False
            self._stop.clear()
            self._snapshot = snapshot
            return True

    def _end(self, snapshot: JobSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def fail(self, message: str) -> None:
        """Show why a task could not start (unless it is running)."""
        with self._lock:
            if not self._snapshot.running:
                self._snapshot = JobSnapshot(state="error", message=message)

    def cancel(self) -> None:
        """Ask a running task to stop after the item it is on."""
        self._stop.set()
