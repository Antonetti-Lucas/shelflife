"""Quality prior from Steam review counts.

The Wilson lower bound turns "positive / total" into a cautious score: 95%
positive over 40,000 reviews beats 100% over 6. The result scales a game's
tag-similarity score, so well-reviewed games rise and poorly reviewed ones fall.
"""

import math

QUALITY_EXPONENT = 2.0  # higher = review score matters more
UNKNOWN_WILSON = 0.6  # no review data: assume a slightly below-average game


def wilson_lower_bound(positive: int, negative: int, z: float = 1.96) -> float | None:
    n = positive + negative
    if n == 0:
        return None
    p = positive / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - margin) / (1 + z * z / n)


def quality_factor(positive: int | None, negative: int | None) -> float:
    """Multiplier in (0, 1]; 0.9 for a game ~95% positive, ~0.5 for ~70%."""
    wilson = None
    if positive is not None and negative is not None:
        wilson = wilson_lower_bound(positive, negative)
    return (UNKNOWN_WILSON if wilson is None else wilson) ** QUALITY_EXPONENT
