"""Recommendations from tag similarity to your taste, scaled by review quality.

Taste profile = tag vectors of your played games, each weighted by how much
you liked it. An explicit rating always wins; for unrated games the engagement
score stands in at reduced strength; a "not interested" counts as a low rating and
a favorite as a top one. A status (playing, played, dropped) is a record only: it
takes a game out of the candidates but does not change what you like.

Candidates come in three lists: games you own but have barely played (backlog),
games you can play through Steam Family Sharing (family), and well-reviewed
games you don't have yet (discover). Each is scored by cosine similarity to the
profile times a quality factor from Steam reviews.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.diary import favorite_ratings, tracked_ids
from shelflife.feedback import NOT_INTERESTED_RATING
from shelflife.models import Game, LibraryEntry
from shelflife.quality import quality_factor
from shelflife.settings import DEFAULT_TASTE_MODE, get_taste_mode
from shelflife.score import engagement_score
from shelflife.tags import compute_idf, cosine, tag_vector

NEUTRAL_RATING = 3.0  # a 3-star rating adds nothing; lower ratings push tags away
IMPLICIT_MAX = 0.5  # unrated games count at most half as much as a 5-star rating
IMPLICIT_ZERO_SCORE = 2.0  # engagement score at or below this adds nothing
IMPLICIT_FULL_SCORE = 5.0  # ...and at or above this reaches IMPLICIT_MAX
MIX_RATING_SHARE = 0.7  # in the mix, a rated game counts 70% by its rating, 30% by how you played it
BACKLOG_MAX_MIN = 60  # owned and played under an hour = backlog


@dataclass(frozen=True)
class Recommendation:
    game: Game
    score: float  # similarity x quality
    similarity: float
    quality: float
    because: list[str]  # played games that pulled this one up
    tags: list[str]  # tags of this game that match your taste
    watch: list[str]  # tags of this game you tend to rate lower


@dataclass(frozen=True)
class Taste:
    profile: dict[str, float]
    explicit_count: int
    implicit_count: int


@dataclass(frozen=True)
class Recommendations:
    backlog: list[Recommendation]
    family: list[Recommendation]
    discover: list[Recommendation]
    taste: Taste
    wishlist: list[Recommendation] = field(default_factory=list)  # unowned games on your wishlist


@dataclass
class _Model:
    vectors: dict[int, dict[str, float]]
    profile: dict[str, float]
    contributors: list[tuple[float, Game, float | None]]
    candidates: list[Game]
    played_titles: set[str]
    owned_titles: set[str]
    family_titles: set[str]
    explicit: int
    implicit: int
    games: list[Game]


@dataclass(frozen=True)
class Scores:
    """Recommendation scores for every game with tags, plus which ones count as
    recommended (unplayed, not dismissed, not a duplicate edition), by list."""

    by_id: dict[int, Recommendation]
    backlog: set[int]
    family: set[int]
    discover: set[int]


def engagement_fraction(entry: LibraryEntry) -> float:
    """How much your play of a game says you liked it, from 0 (nothing) to 1 (a lot):
    the engagement score between IMPLICIT_ZERO_SCORE and IMPLICIT_FULL_SCORE."""
    span = IMPLICIT_FULL_SCORE - IMPLICIT_ZERO_SCORE
    fraction = (engagement_score(entry).total - IMPLICIT_ZERO_SCORE) / span
    return min(max(fraction, 0.0), 1.0)


def taste_weight(
    entry: LibraryEntry | None, rating: float | None, mode: str = DEFAULT_TASTE_MODE
) -> float:
    """How strongly a game shapes your taste: -1 (hated) to +1 (loved).

    `mode` says what to learn from:
    - "ratings": only your own word. A game with no rating counts for nothing.
    - "engagement": only how you played (engagement score, which includes how
      recently). Ratings are ignored; play cannot say "disliked", so this is 0 to 1.
    - "mix": both. A rated game is MIX_RATING_SHARE its rating and the rest how you
      played it (play lifts a rating, it never lowers it); an unrated game counts by
      how you played it, at most IMPLICIT_MAX.
    """
    if mode == "engagement":
        return 0.0 if entry is None else engagement_fraction(entry)
    rated = None if rating is None else (rating - NEUTRAL_RATING) / (5.0 - NEUTRAL_RATING)
    if mode == "ratings":
        return rated or 0.0
    if rated is not None:
        if entry is None:  # played elsewhere: nothing but your rating to go on
            return rated
        return MIX_RATING_SHARE * rated + (1 - MIX_RATING_SHARE) * engagement_fraction(entry)
    return 0.0 if entry is None else IMPLICIT_MAX * engagement_fraction(entry)


_EDITION_WORDS = re.compile(
    r"\b(remastered|remaster|enhanced( edition)?|definitive edition|special edition|"
    r"complete edition|deluxe edition|game of the year( edition)?|goty( edition)?|director'?s cut|hd|"
    r"legacy)\b"  # Steam renames the old app "(Legacy)" when a remaster replaces it
)


def _base_name(name: str) -> str:
    """Title without trademark symbols or edition words, so a remaster of a
    game you already own isn't recommended as something new."""
    name = name.lower().replace("™", "").replace("®", "")
    name = _EDITION_WORDS.sub(" ", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def _label(game: Game, rating: float | None, similarity: float) -> str:
    name = game.name.replace("™", "").replace("®", "")
    if rating is not None:
        played = f"rated {rating:g}"
    elif game.family_shared and game.library_entry.playtime_total_min == 0:
        entry = game.library_entry
        played = "family game"
        if entry.achievements_total:
            played += f", {entry.achievements_unlocked}/{entry.achievements_total} achievements"
    else:
        played = f"{game.library_entry.playtime_total_hours:.0f}h played"
    return f"{name} ({played}, {similarity:.0%} similar)"


def is_played(entry: LibraryEntry | None, family: bool) -> bool:
    """Whether you've really played a game. Steam reports no playtime for family
    games, so for those any unlocked achievement counts as having played."""
    if entry is None:
        return False
    if entry.playtime_total_min >= BACKLOG_MAX_MIN:
        return True
    return family and (entry.achievements_unlocked or 0) > 0


def _build_model(
    session: Session, ratings: dict[int, float], not_interested: dict[int, str]
) -> _Model:
    effective = dict(ratings)
    for app_id, stand_in in favorite_ratings(session).items():
        effective.setdefault(app_id, stand_in)
    tracked = tracked_ids(session)
    for app_id in not_interested:
        effective.setdefault(app_id, NOT_INTERESTED_RATING)
    mode = get_taste_mode(session)

    games = list(session.scalars(select(Game).where(Game.type == "game")))
    # Rarity is measured on your own library, not on the pool picked by tag.
    idf = compute_idf(g.tags for g in games if g.library_entry is not None)
    vectors = {g.steam_app_id: tag_vector(g.tags, idf) for g in games}

    model = _Model(vectors, {}, [], [], set(), set(), set(), 0, 0, games)
    for g in games:
        entry = g.library_entry
        family = bool(g.family_shared)
        rating = effective.get(g.steam_app_id)
        if entry is not None:
            (model.family_titles if family else model.owned_titles).add(_base_name(g.name))

        if rating is None and not is_played(entry, family) and g.steam_app_id not in tracked:
            if vectors[g.steam_app_id]:
                model.candidates.append(g)
            continue

        model.played_titles.add(_base_name(g.name))
        weight = taste_weight(entry, rating, mode)
        if weight == 0:
            continue
        counted_as_rated = rating is not None and mode != "engagement"
        model.explicit += counted_as_rated
        model.implicit += not counted_as_rated
        # The real rating only (a favorite's stand-in 5 must not read "rated 5"), and none
        # when ratings are being ignored.
        shown_rating = ratings.get(g.steam_app_id) if mode != "engagement" else None
        model.contributors.append((weight, g, shown_rating))
        for tag, value in vectors[g.steam_app_id].items():
            model.profile[tag] = model.profile.get(tag, 0.0) + weight * value
    return model


def top_taste_tags(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str],
    n: int = 8,
) -> list[str]:
    """The tags your taste profile leans toward most; seeds the discovery pool."""
    profile = _build_model(session, ratings, not_interested).profile
    ranked = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, v in ranked[:n] if v > 0]


def taste_profile(
    session: Session, ratings: dict[int, float], not_interested: dict[int, str]
) -> Taste:
    model = _build_model(session, ratings, not_interested)
    return Taste(model.profile, model.explicit, model.implicit)


def explain(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str],
    app_id: int,
) -> Recommendation | None:
    """How well one specific game matches your taste, and why.

    Works for any game with tags, including ones you've already played; None when
    the game has no usable tags yet.
    """
    model = _build_model(session, ratings, not_interested)
    game = session.get(Game, app_id)
    if game is None or not model.vectors.get(app_id):
        return None
    return _score(game, model)


def _score(cand: Game, model: _Model) -> Recommendation:
    vec = model.vectors[cand.steam_app_id]
    similarity = cosine(model.profile, vec)

    pulls = sorted(
        (
            (sim * w, sim, g, r)
            for w, g, r in model.contributors
            if w > 0 and g.steam_app_id != cand.steam_app_id  # not compared with itself
            for sim in [cosine(model.vectors[g.steam_app_id], vec)]
        ),
        key=lambda p: p[0],
        reverse=True,
    )[:2]

    by_tag = sorted(((model.profile.get(t, 0.0) * v, t) for t, v in vec.items()), reverse=True)
    best = by_tag[0][0] if by_tag else 0.0

    quality = quality_factor(cand.reviews_positive, cand.reviews_negative)
    return Recommendation(
        game=cand,
        score=similarity * quality,
        similarity=similarity,
        quality=quality,
        because=[_label(g, r, sim) for c, sim, g, r in pulls if c > 0],
        tags=[t for c, t in by_tag[:3] if c > 0],
        watch=[t for c, t in by_tag[-1:] if c < 0 and -c > 0.1 * best],
    )


def _classify(model: _Model) -> tuple[list[Game], list[Game], list[Game]]:
    """Split the candidates into (backlog, family, discover), dropping games whose
    other edition you already have or have played."""
    backlog: list[Game] = []
    family: list[Game] = []
    discover: list[Game] = []
    for cand in model.candidates:
        base = _base_name(cand.name)
        if base in model.played_titles:
            continue
        if cand.library_entry is None:
            # A remaster of something you already have access to isn't a discovery.
            if base not in model.owned_titles and base not in model.family_titles:
                discover.append(cand)
        elif cand.family_shared:
            if base not in model.owned_titles:
                family.append(cand)
        else:
            backlog.append(cand)
    return backlog, family, discover


def score_all(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str] | None = None,
) -> Scores:
    """Score every game with tags once, and say which count as recommended."""
    model = _build_model(session, ratings, not_interested or {})
    backlog, family, discover = _classify(model)
    return Scores(
        by_id={
            g.steam_app_id: _score(g, model)
            for g in model.games
            if model.vectors[g.steam_app_id]
        },
        backlog={g.steam_app_id for g in backlog},
        family={g.steam_app_id for g in family},
        discover={g.steam_app_id for g in discover},
    )


def recommend(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str] | None = None,
    limit: int = 10,
    wishlist_ids: Collection[int] | None = None,
) -> Recommendations:
    """The recommendation lists. Games in `wishlist_ids` that you do not own come out of
    "discover" into their own list, so each game is listed once."""
    model = _build_model(session, ratings, not_interested or {})
    backlog_games, family_games, discover_games = _classify(model)
    wished = set(wishlist_ids or ())
    wishlist_games = [g for g in discover_games if g.steam_app_id in wished]
    discover_games = [g for g in discover_games if g.steam_app_id not in wished]
    backlog = [_score(g, model) for g in backlog_games]
    family = [_score(g, model) for g in family_games]
    discover = [_score(g, model) for g in discover_games]
    wishlist = [_score(g, model) for g in wishlist_games]

    def top(recs: list[Recommendation]) -> list[Recommendation]:
        recs.sort(key=lambda r: r.score, reverse=True)
        seen: set[str] = set()
        unique = []
        for r in recs:  # you may have two editions of one game; list it once
            base = _base_name(r.game.name)
            if base not in seen:
                seen.add(base)
                unique.append(r)
        return unique[:limit]

    return Recommendations(
        backlog=top(backlog),
        family=top(family),
        discover=top(discover),
        taste=Taste(model.profile, model.explicit, model.implicit),
        wishlist=top(wishlist),
    )
