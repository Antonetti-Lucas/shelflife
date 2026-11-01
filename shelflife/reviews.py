"""Review counts, and how long reviewers played, from Valve's IUserReviewsService (no key needed)."""

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


def _get(app_id: int, timeout: float, **params) -> dict:
    reviews_throttle.wait()
    resp = requests.get(
        REVIEWS_URL,
        params={"appid": app_id, "languages": "all", "purchase_type": 1, **params},
        headers=HEADERS,
        timeout=timeout,
    )
    if resp.status_code == 429:
        raise RateLimited("Steam reviews endpoint is rate limiting us; try again later.")
    resp.raise_for_status()
    return resp.json().get("response", {})


def fetch_review_counts(app_id: int, timeout: float = 15) -> ReviewCounts | None:
    """All-language review totals; None when the app has no summary."""
    # One review per page: we only want the summary, not the review text.
    summary = _get(app_id, timeout, num_per_page=1).get("query_summary", {})
    if "total_positive" not in summary:
        return None
    return ReviewCounts(summary["total_positive"], summary["total_negative"])


# ---- how long players play a game ----
#
# Each review says how long its author has played the game. Those hours give every game its own
# yardstick: "you played more than 70% of the people who reviewed it" means the same for a two-hour
# game and an endless one. Checked on five games (see PLANS.md): 100 reviews place a typical player
# within about +-7 points, 300 within +-3; the first pages come most helpful first and lean toward
# long-time players, so more pages also make the sample fairer. Negative reviewers of a game with an
# ending play far less (Celeste: 4 hours against 26), and samples hold two or three times more of
# them than the real share, so each side is weighed by the game's real share of positive reviews.
# Only the percentiles below are kept, never a reviewer's id or text.

PLAYER_PERCENTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
PLAYER_PAGES = 5  # 100 reviews a page
# Reviews from this many days back. Left out, Steam gives only the last year's, which for an older
# game can be a handful (Zuma's Revenge: 19 of its 1,721); ten years fills the pages.
PLAYER_DAY_RANGE = 3650
MIN_PLAYER_SAMPLE = 20  # fewer reviewers with hours than this: not worth keeping


@dataclass(frozen=True)
class PlayerHours:
    percentiles: list[float]  # hours at each of PLAYER_PERCENTILES
    sample: int  # reviewers they come from


def weighted_percentiles(reviews: list[tuple[float, bool]], positive: int | None, negative: int | None) -> list[float]:
    """Hours at each of PLAYER_PERCENTILES among `reviews` (hours, voted up), each side weighed so
    positive and negative reviewers count in the game's real proportions (when they are known
    and both sides are in the sample)."""
    ups = sum(1 for _, up in reviews if up)
    downs = len(reviews) - ups
    weight = {True: 1.0, False: 1.0}
    if positive is not None and negative is not None and positive + negative > 0 and ups and downs:
        share = positive / (positive + negative)
        weight = {True: share / ups, False: (1 - share) / downs}
    ranked = sorted(reviews)
    total = sum(weight[up] for _, up in ranked)
    out, seen, i = [], 0.0, 0
    for p in PLAYER_PERCENTILES:
        while i < len(ranked) - 1 and seen + weight[ranked[i][1]] < p * total:
            seen += weight[ranked[i][1]]
            i += 1
        out.append(ranked[i][0])
    return out


def fetch_player_hours(
    app_id: int, positive: int | None = None, negative: int | None = None, pages: int = PLAYER_PAGES, timeout: float = 15
) -> PlayerHours | None:
    """How long the game's reviewers have played it, as percentiles; None with too few reviewers.
    `positive` and `negative` are the game's review counts, to weigh the two sides."""
    reviews: list[tuple[float, bool]] = []
    cursor = "*"
    for _ in range(pages):
        data = _get(app_id, timeout, num_per_page=100, cursor=cursor, filter="all", day_range=PLAYER_DAY_RANGE)
        page = data.get("reviews") or []
        for r in page:
            hours = (r.get("author") or {}).get("playtime_forever", 0) / 60
            if hours > 0:  # no hours on record: says nothing about how long the game lasts
                reviews.append((hours, bool(r.get("voted_up"))))
        new = data.get("cursor")
        if not page or not new or new == cursor:  # a short page is not always the last one
            break
        cursor = new
    if len(reviews) < MIN_PLAYER_SAMPLE:
        return None
    return PlayerHours(weighted_percentiles(reviews, positive, negative), len(reviews))
