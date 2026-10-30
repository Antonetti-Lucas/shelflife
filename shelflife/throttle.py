"""Minimum-interval throttle so we stay polite to third-party sites."""

import time


class RateLimited(Exception):
    """The remote site told us to slow down; stop and retry later."""


class Throttle:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        remaining = self.min_interval - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()
