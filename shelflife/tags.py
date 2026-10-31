"""Turn a game's community tags into a weighted vector for similarity.

Three filters keep irrelevant or wrong tags from dominating:
  1. vote share: a tag's weight is its votes relative to the game's top tag,
     and only the top TOP_N tags count;
  2. library rarity (IDF): a tag on nearly every game you own says little;
  3. a stoplist of tags that aren't about how a game plays.
"""

import math
from typing import Iterable

TOP_N = 15
# How much a tag's relevance to its game matters. A tag's weight is its vote share
# (relative to the game's top tag) raised to this power: 1 is a straight line, higher
# makes minor tags count much less (at 2, a tag with half the top tag's votes counts
# a quarter as much). 2 because, on the 18 ratings the owner gave, it ranked games
# better than 1 (leave-one-out rank correlation +0.29 vs +0.22; a small sample).
RELEVANCE_POWER = 2.0

STOPLIST = frozenset(
    {
        "Early Access",
        "Free to Play",
        "Great Soundtrack",
        "Controller",
        "Full controller support",
        "Steam Achievements",
        "Steam Cloud",
    }
)


def tag_weights(tags: dict[str, int]) -> dict[str, float]:
    """Vote share relative to the game's top tag, for its top tags only."""
    ranked = sorted(
        ((t, v) for t, v in tags.items() if t not in STOPLIST),
        key=lambda kv: kv[1],
        reverse=True,
    )[:TOP_N]
    if not ranked or ranked[0][1] <= 0:
        return {}
    top = ranked[0][1]
    return {t: (v / top) ** RELEVANCE_POWER for t, v in ranked}


def compute_idf(all_tags: Iterable[dict[str, int]]) -> dict[str, float]:
    """Rarity of each tag across the library; ~0 for tags on almost every game."""
    counts: dict[str, int] = {}
    n = 0
    for tags in all_tags:
        weights = tag_weights(tags)
        if not weights:
            continue
        n += 1
        for t in weights:
            counts[t] = counts.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 1)) for t, c in counts.items()}


def tag_vector(tags: dict[str, int], idf: dict[str, float]) -> dict[str, float]:
    return {t: w * idf.get(t, 0.0) for t, w in tag_weights(tags).items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity; a may contain negative weights (disliked tags)."""
    dot = sum(a[t] * b[t] for t in a.keys() & b.keys())
    na = math.sqrt(sum(x * x for x in a.values()))
    nb = math.sqrt(sum(x * x for x in b.values()))
    return dot / (na * nb) if na and nb else 0.0
