"""Recommendations from how well a game fits what you liked, scaled by review quality.

How much you liked each game you played is one number from -1 to +1 (`taste_weight`): an
explicit rating always counts; for unrated games the engagement score stands in, scaled by
what play has been worth in your own rated games (`learn_play_value`); a "not interested"
counts as a low rating and a favorite as a top one. A status (playing,
played, dropped) is a record only: it takes a game out of the candidates but does not
change what you like.

Those numbers are what shelflife/learned.py learns from: it predicts how much you would like
any game from its tags and the words of its store text (it is fitted to your games one by
one, so a game very close to a favourite stays close). The tag profile (the sum of your
games' tags times those numbers) is kept for the My taste page, for the tags a pick matches,
and for seeding the search for new games.

Candidates come in three lists: games you own but have barely played (backlog),
games you can play through Steam Family Sharing (family), and well-reviewed
games you don't have yet (discover). Each is scored by that prediction times a quality
factor from Steam reviews.
"""

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from functools import cached_property

from sqlalchemy import select
from sqlalchemy.orm import Session

from shelflife.diary import favorite_ratings, tracked_ids
from shelflife.feedback import NOT_INTERESTED_RATING
from shelflife.learned import (
    MATCH_SCALE,
    TEXT_WEIGHT,
    LearnedTaste,
    Vector,
    fit,
    fit_cached,
    likeness,
    text_index,
    unit_tag_vectors,
)
from shelflife.models import Game, LibraryEntry
from shelflife.quality import quality_factor
from shelflife.settings import DEFAULT_TASTE_MODE, get_taste_mode
from shelflife.score import engagement_score
from shelflife.tags import compute_idf, cosine, tag_vector

NEUTRAL_RATING = 3.0  # a 3-star rating adds nothing; lower ratings push tags away
IMPLICIT_MAX = 0.5  # unrated games count at most half as much as a 5-star rating
IMPLICIT_ZERO_SCORE = 2.0  # engagement score at or below this adds nothing
IMPLICIT_FULL_SCORE = 5.0  # ...and at or above this reaches IMPLICIT_MAX
PLAY_PRIOR = 2.0  # the starting guess for what play is worth (IMPLICIT_MAX) counts as this many fully played games
BACKLOG_MAX_MIN = 60  # owned and played under an hour = backlog


class _Lazy(Sequence):
    """A list worked out the first time it is read. The reasons behind a pick take a pass over
    your games each, and only the picks on screen are ever read."""

    def __init__(self, make: Callable[[], list[str]]):
        self._make = make
        self._items: list[str] | None = None

    def _get(self) -> list[str]:
        if self._items is None:
            self._items = self._make()
        return self._items

    def __len__(self) -> int:
        return len(self._get())

    def __getitem__(self, index):
        return self._get()[index]

    def __eq__(self, other) -> bool:
        return list(self) == list(other) if isinstance(other, (list, tuple, Sequence)) else NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return repr(self._get())


@dataclass(frozen=True)
class Recommendation:
    game: Game
    score: float  # predicted liking x quality
    similarity: float  # predicted liking, 0 to 1: how well the game fits you
    quality: float
    because: Sequence[str]  # played games that pulled this one up
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
    session: Session | None = None
    # Every game with a verdict (a rating, or play that counts), as (id, how much you liked it).
    # A 3-star rating is a verdict of zero, so unlike `contributors` it is kept.
    training: list[tuple[int, float]] = field(default_factory=list)
    play_value: float = IMPLICIT_MAX  # what a fully played, unrated game counts as (learned in the mix)
    # For the evaluation (held_out_liking): in the mix, the (play, rating) pairs play_value was
    # learned from, and how much each unrated game with tags was played.
    play_pairs: dict[int, tuple[float, float]] = field(default_factory=dict)
    unrated_play: dict[int, float] = field(default_factory=dict)

    @cached_property
    def tag_units(self) -> dict[int, Vector]:
        return unit_tag_vectors(self.vectors)

    @cached_property
    def text_units(self) -> dict[int, Vector]:
        return text_index(self.session) if self.session is not None else {}

    @cached_property
    def beta(self) -> float:
        """How much the store text counts: half, once any game has its text; nothing before."""
        return TEXT_WEIGHT if self.text_units else 0.0

    @cached_property
    def learned(self) -> LearnedTaste:
        training = sorted(self.training)
        key = (
            tuple(training),
            tuple((i, tuple(sorted(self.tag_units[i].items()))) for i, _ in training),
            self.beta,
            len(self.text_units),
            sum(len(v) for v in self.text_units.values()),
        )
        return fit_cached(key, training, self.tag_units, self.text_units, self.beta)

    def liking(self, app_id: int) -> float:
        """How much you would like this game, -1 to 1 (0 when it has no tags)."""
        if not self.vectors.get(app_id):
            return 0.0
        return self.learned.predict(self.tag_units[app_id], self.text_units.get(app_id))


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


def learn_play_value(pairs: Sequence[tuple[float, float]]) -> float:
    """What a fully played game is worth as a rating (0 to 1), learned from the games you
    both rated and played: `pairs` is (how much you played it, 0 to 1; your rating as -1 to 1).
    A line through zero, so a game you barely played says nothing, started from IMPLICIT_MAX and
    pulled toward your own games as they come in: if the games you play most are the ones you
    rate high, play counts for more; if you play games you rate low, it counts for less."""
    sxx = sum(e * e for e, _ in pairs)
    sxy = sum(e * r for e, r in pairs)
    value = (sxy + PLAY_PRIOR * IMPLICIT_MAX) / (sxx + PLAY_PRIOR)
    return min(max(value, 0.0), 1.0)


def taste_weight(
    entry: LibraryEntry | None,
    rating: float | None,
    mode: str = DEFAULT_TASTE_MODE,
    play_value: float = IMPLICIT_MAX,
) -> float:
    """How strongly a game shapes your taste: -1 (hated) to +1 (loved).

    `mode` says what to learn from:
    - "ratings": only your own word. A game with no rating counts for nothing.
    - "engagement": only how you played (engagement score, which includes how
      recently). Ratings are ignored; play cannot say "disliked", so this is 0 to 1.
    - "mix": both. A rated game counts by its rating, as it stands; an unrated game you played
      counts as the rating its play suggests: `play_value` (learned from your rated games by
      `learn_play_value`) times how much you played it.
    """
    if mode == "engagement":
        return 0.0 if entry is None else engagement_fraction(entry)
    rated = None if rating is None else (rating - NEUTRAL_RATING) / (5.0 - NEUTRAL_RATING)
    if mode == "ratings" or rated is not None:
        return rated or 0.0
    return 0.0 if entry is None else play_value * engagement_fraction(entry)


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
    session: Session, ratings: dict[int, float], not_interested: dict[int, str], mode: str | None = None
) -> _Model:
    effective = dict(ratings)
    for app_id, stand_in in favorite_ratings(session).items():
        effective.setdefault(app_id, stand_in)
    tracked = tracked_ids(session)
    for app_id in not_interested:
        effective.setdefault(app_id, NOT_INTERESTED_RATING)
    mode = mode or get_taste_mode(session)

    games = list(session.scalars(select(Game).where(Game.type == "game")))
    # Rarity is measured on your own library, not on the pool picked by tag.
    idf = compute_idf(g.tags for g in games if g.library_entry is not None)
    vectors = {g.steam_app_id: tag_vector(g.tags, idf) for g in games}

    model = _Model(vectors, {}, [], [], set(), set(), set(), 0, 0, games, session=session)
    play_value = IMPLICIT_MAX
    if mode == "mix":
        model.play_pairs = {
            g.steam_app_id: (
                engagement_fraction(g.library_entry),
                (effective[g.steam_app_id] - NEUTRAL_RATING) / (5.0 - NEUTRAL_RATING),
            )
            for g in games
            if g.library_entry is not None and g.steam_app_id in effective
        }
        play_value = learn_play_value(list(model.play_pairs.values()))
    model.play_value = play_value
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
        if mode == "mix" and rating is None and entry is not None and vectors[g.steam_app_id]:
            model.unrated_play[g.steam_app_id] = engagement_fraction(entry)
        weight = taste_weight(entry, rating, mode, play_value)
        if vectors[g.steam_app_id] and (weight != 0 or (rating is not None and mode != "engagement")):
            model.training.append((g.steam_app_id, weight))  # a rating of 3 stars says "neutral", not "unknown"
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


def held_out_liking(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str],
    app_ids: Collection[int],
    mode: str | None = None,
) -> dict[int, float]:
    """For checking the model (tools/evaluate.py): how much you would like each game in
    `app_ids`, predicted with everything known about that game left out (its rating, favorite,
    play, and its part in learning what play is worth). Games without tags are skipped."""
    model = _build_model(session, ratings, not_interested, mode)
    known: dict[tuple[int, int], float] = {}

    def kernel(a: int, b: int) -> float:
        key = (a, b) if a <= b else (b, a)
        if key not in known:
            known[key] = likeness(
                model.tag_units[a], model.text_units.get(a), model.tag_units[b], model.text_units.get(b), model.beta
            )
        return known[key]

    out = {}
    for app_id in app_ids:
        if not model.vectors.get(app_id):
            continue
        training = [(i, w) for i, w in model.training if i != app_id]
        if app_id in model.play_pairs:  # its rating helped learn what play is worth: learn it again without
            value = learn_play_value([p for i, p in model.play_pairs.items() if i != app_id])
            training = [(i, w) for i, w in training if i not in model.unrated_play] + [
                (i, value * f) for i, f in model.unrated_play.items() if i != app_id and value * f != 0
            ]
        taste = fit(training, model.tag_units, model.text_units, model.beta, kernel=kernel)
        out[app_id] = taste.predict(model.tag_units[app_id], model.text_units.get(app_id))
    return out


def _because(cand: Game, model: _Model) -> list[str]:
    """The two games you liked that this one is closest to."""
    tags, text = model.tag_units[cand.steam_app_id], model.text_units.get(cand.steam_app_id)
    pulls = sorted(
        (
            (sim * w, sim, g, r)
            for w, g, r in model.contributors
            if w > 0 and g.steam_app_id != cand.steam_app_id  # not compared with itself
            for sim in [
                likeness(tags, text, model.tag_units[g.steam_app_id], model.text_units.get(g.steam_app_id), model.beta)
            ]
        ),
        key=lambda p: p[0],
        reverse=True,
    )[:2]
    return [_label(g, r, sim) for c, sim, g, r in pulls if c > 0]


def _score(cand: Game, model: _Model) -> Recommendation:
    vec = model.vectors[cand.steam_app_id]
    # Below 0: it resembles games you did not like.
    similarity = min(model.liking(cand.steam_app_id) * MATCH_SCALE, 1.0)

    by_tag = sorted(((model.profile.get(t, 0.0) * v, t) for t, v in vec.items()), reverse=True)
    best = by_tag[0][0] if by_tag else 0.0

    quality = quality_factor(cand.reviews_positive, cand.reviews_negative)
    return Recommendation(
        game=cand,
        score=similarity * quality,
        similarity=similarity,
        quality=quality,
        because=_Lazy(lambda: _because(cand, model)),
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
