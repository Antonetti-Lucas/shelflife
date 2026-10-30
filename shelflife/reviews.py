"""Review counts from Valve's documented IUserReviewsService (no key needed)."""

from dataclasses import dataclass

import requests

from shelflife.store import HEADERS
from shelflife.throttle import RateLimited, Throttle

REVIEWS_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"

reviews_throttle = Throttle(1.0)


@dataclass(frozen=True)
class ReviewCounts:
    positive: int
    negative: int


def fetch_review_counts(app_id: int, timeout: float = 15) -> ReviewCounts | None:
    """All-language review totals; None when the app has no summary."""
    reviews_throttle.wait()
    resp = requests.get(
        REVIEWS_URL,
        # One review per page: we only want the summary, not the review text.
        params={"appid": app_id, "num_per_page": 1, "languages": "all", "purchase_type": 1},
        headers=HEADERS,
        timeout=timeout,
    )
    if resp.status_code == 429:
        raise RateLimited("Steam reviews endpoint is rate limiting us; try again later.")
    resp.raise_for_status()

    summary = resp.json().get("response", {}).get("query_summary", {})
    if "total_positive" not in summary:
        return None
    return ReviewCounts(summary["total_positive"], summary["total_negative"])
