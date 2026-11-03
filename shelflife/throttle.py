"""Minimum-interval throttle so we stay polite to third-party sites."""

import threading
import time


class RateLimited(Exception):
    """The remote site told us to slow down; stop and retry later."""


class Throttle:
    """Callers in different threads (a sync, a search, a game being added) take turns."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            remaining = self.min_interval - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)
            self._last = time.monotonic()
