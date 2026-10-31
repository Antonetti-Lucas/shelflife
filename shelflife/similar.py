"""Games similar to one game, by their community tags.

Used by the "Games like this" page (Explore ranked by similarity to a game) and by
"Find new games like this" (a SteamSpy search that keeps only close matches). The
similarity is the same tag measure as the recommender's: each game's tags weighted
by how relevant they are to it and how rare they are, compared by cosine.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.learned import TEXT_WEIGHT, Vector, likeness, unit
from shelflife.models import Game
from shelflife.quality import quality_factor
from shelflife.recommend import Recommendation
from shelflife.tags import compute_idf, cosine, tag_vector

MIN_SIMILARITY = 0.15  # a new game must be at least this close to count as "like" the other
SEARCH_TAGS = 3  # how many of a game's tags a search for similar games uses


def tag_idf(session: Session) -> dict[str, float]:
    """Tag rarity across every game with tags (library and pool), so common tags such
    as "Singleplayer" count for little."""
    games = session.scalars(select(Game).where(Game.type == "game"))
    return compute_idf(g.tags for g in games if g.tags)


def reference_vector(game: Game, idf: dict[str, float]) -> dict[str, float]:
    return tag_vector(game.tags or {}, idf)


def search_tags(game: Game, idf: dict[str, float], n: int = SEARCH_TAGS) -> list[str]:
    """The tags that most set a game apart, to search SteamSpy for similar ones."""
    ranked = sorted(reference_vector(game, idf).items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, v in ranked[:n] if v > 0]


def similarity(reference: dict[str, float], tags: dict[str, int] | None, idf: dict[str, float]) -> float:
    return cosine(reference, tag_vector(tags or {}, idf))


def similar_to(
    reference_game: Game,
    candidates: Iterable[Game],
    idf: dict[str, float],
    texts: dict[int, Vector] | None = None,
) -> list[Recommendation]:
    """The candidates ranked by how like `reference_game` they are (similarity x review
    quality, best first). Each carries the tags it shares with it. With `texts` (the word
    vectors of the games' store text, from learned.text_index) the likeness is half tags, half
    words, as in the recommendations; a game without text is judged on its tags alone."""
    ref = reference_vector(reference_game, idf)
    ref_unit = unit(ref)
    ref_text = (texts or {}).get(reference_game.steam_app_id)
    beta = TEXT_WEIGHT if ref_text else 0.0
    out = []
    for g in candidates:
        if g.steam_app_id == reference_game.steam_app_id or not g.tags:
            continue
        vec = tag_vector(g.tags, idf)
        sim = likeness(ref_unit, ref_text, unit(vec), (texts or {}).get(g.steam_app_id), beta)
        shared = sorted(ref.keys() & vec.keys(), key=lambda t: ref[t] * vec[t], reverse=True)
        quality = quality_factor(g.reviews_positive, g.reviews_negative)
        out.append(
            Recommendation(
                game=g,
                score=sim * quality,
                similarity=sim,
                quality=quality,
                because=[],
                tags=shared[:3],
                watch=[],
            )
        )
    out.sort(key=lambda r: r.score, reverse=True)
    return out
