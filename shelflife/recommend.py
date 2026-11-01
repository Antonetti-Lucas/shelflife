"""Recommendations from how well a game fits what you liked, scaled by review quality.

Two things say how much you liked a game, and the taste mode (My taste page) picks which count:
- your **ratings**: `(rating - 3) / 2`, from -1 (1 star) to +1 (5 stars). In "Mix of both" a
  favorite you did not rate counts as 5 stars and "not interested" as 2; in "Your ratings only"
  only real star ratings count.
- your **play** (shelflife/play.py): how much you played a game compared with the other games
  you played, from -0.5 (far less than usual) to +1 (far more); 0 is your typical game.

shelflife/learned.py learns a taste from each: it predicts, from a game's tags and the words of
its store text, how much you would like it. "Your ratings only" uses the taste learned from
ratings, "How you play only" the one learned from play, and "Mix of both" adds the two, each
weighted by how well it predicts your own ratings (worked out with every rated game left out
in turn, see `blend`). So in the mix every game teaches through everything known about it: a
game you rated and played teaches through both.

A status (playing, played, dropped) is a record only: it takes a game out of the candidates
but does not change what you like.

The tag profile (the sum of your games' tags times how much each counts) is kept for the My
taste page, for the tags a pick matches, and for seeding the search for new games.

Candidates come in four lists: games you own but have barely played (backlog), games you can
play through Steam Family Sharing (family), your wishlist, and well-reviewed games you don't
have yet (discover). Each is scored by the prediction times a quality factor from Steam reviews.
"""

import math
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
from shelflife.play import BACKLOG_MAX_MIN, PlayScale, PlayScore, is_played
from shelflife.quality import quality_factor
from shelflife.settings import get_hide_adult, get_taste_mode
from shelflife.tags import compute_idf, is_adult, tag_vector

__all__ = ["BACKLOG_MAX_MIN", "is_played"]  # also imported from here by other modules

NEUTRAL_RATING = 3.0  # a 3-star rating says "an ordinary game for you"; lower ratings push away
# The mix's share for each taste before your ratings say otherwise (two thirds ratings, one third
# play); that guess counts as BLEND_PRIOR rated games, so a few ratings move it only a little.
BLEND_START = (2 / 3, 1 / 3)
BLEND_PRIOR = 5.0
SPREAD_FLOOR = 0.05  # a tenth of a star: predictions that vary less than this are treated as saying nothing
CORRELATION_FLOOR = 0.1  # a taste is never scaled away entirely, even when it predicts poorly
MIN_GAMES_TO_LEARN = 5  # fewer rated (or played) games than this say nothing about how to weigh the tastes


def rating_weight(rating: float) -> float:
    """A star rating as -1 (1 star) to +1 (5 stars); 3 stars is 0."""
    return (rating - NEUTRAL_RATING) / (5.0 - NEUTRAL_RATING)


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
    games: list[Game]
    mode: str
    session: Session | None = None
    hide_adult: bool = False  # leave games for adults out of the lists
    candidates: list[Game] = field(default_factory=list)
    played_titles: set[str] = field(default_factory=set)
    owned_titles: set[str] = field(default_factory=set)
    family_titles: set[str] = field(default_factory=set)
    # What each taste learns from: (game id, how much you liked it). From your ratings (-1 to +1;
    # a 3-star rating is a 0 that still counts) and from your play (-0.5 to +1).
    rating_training: list[tuple[int, float]] = field(default_factory=list)
    play_training: list[tuple[int, float]] = field(default_factory=list)
    play_scores: dict[int, PlayScore] = field(default_factory=dict)
    shown_ratings: dict[int, float] = field(default_factory=dict)  # real ratings, for the reasons on a card
    games_by_id: dict[int, Game] = field(default_factory=dict)

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

    def _learn(self, training: list[tuple[int, float]]) -> LearnedTaste:
        training = sorted(training)
        key = (
            tuple(training),
            tuple((i, tuple(sorted(self.tag_units[i].items()))) for i, _ in training),
            self.beta,
            len(self.text_units),
            sum(len(v) for v in self.text_units.values()),
        )
        return fit_cached(key, training, self.tag_units, self.text_units, self.beta, held_out=True)

    @cached_property
    def from_ratings(self) -> LearnedTaste:
        return self._learn(self.rating_training if self.mode != "engagement" else [])

    @cached_property
    def from_play(self) -> LearnedTaste:
        return self._learn(self.play_training if self.mode != "ratings" else [])

    def _predict(self, taste: LearnedTaste, app_id: int, left_out: bool = False) -> float:
        """A taste's prediction for a game; with `left_out`, as if that game had not been learned from."""
        if left_out and app_id in taste.held_out:
            return taste.held_out[app_id][0]
        return taste.predict(self.tag_units[app_id], self.text_units.get(app_id))

    def rated_pairs(self, skip: int | None = None) -> list[tuple[float, float, float]]:
        """For every rated game: (what the taste from ratings predicts for it, what the taste from
        play predicts, your rating as it was learned), each prediction made without that game."""
        return [
            (predicted, self._predict(self.from_play, app_id, left_out=True), target)
            for app_id, (predicted, target) in self.from_ratings.held_out.items()
            if app_id != skip
        ]

    def played_pairs(self, skip: int | None = None) -> list[tuple[float, float]]:
        """For every played game: (what the taste from play predicts for it without it, its play as learned)."""
        return [pair for app_id, pair in self.from_play.held_out.items() if app_id != skip]

    @cached_property
    def blend(self) -> tuple[float, float]:
        """How much each taste counts: (from ratings, from play)."""
        return blend(self.mode, self.rated_pairs(), self.played_pairs())

    def liking(self, app_id: int) -> float:
        """How much more (or less) than your usual game you would like this one, on the scale it was
        learned on (+1 = two stars above your usual); 0 when it has no tags or nothing is known."""
        if not self.vectors.get(app_id):
            return 0.0
        by_ratings, by_play = self.blend
        value = 0.0
        if by_ratings:
            value += by_ratings * self._predict(self.from_ratings, app_id)
        if by_play:
            value += by_play * self._predict(self.from_play, app_id)
        return value

    @cached_property
    def weights(self) -> dict[int, float]:
        """How much each game shapes the tag profile: its rating and its play, weighted as the tastes are."""
        by_ratings, by_play = self.blend
        out: dict[int, float] = {}
        for app_id, w in self.rating_training if by_ratings else ():
            out[app_id] = out.get(app_id, 0.0) + by_ratings * w
        for app_id, w in self.play_training if by_play else ():
            out[app_id] = out.get(app_id, 0.0) + by_play * w
        return out

    @cached_property
    def contributors(self) -> list[tuple[float, Game, float | None]]:
        shown = self.shown_ratings if self.mode != "engagement" else {}
        return [(w, self.games_by_id[i], shown.get(i)) for i, w in self.weights.items() if w != 0]

    @cached_property
    def profile(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for app_id, w in self.weights.items():
            for tag, value in self.vectors[app_id].items():
                out[tag] = out.get(tag, 0.0) + w * value
        return out

    @property
    def explicit(self) -> int:
        """Games learned from your ratings."""
        return len(self.rating_training) if self.blend[0] else 0

    @property
    def implicit(self) -> int:
        """Games learned from your play alone (not also rated)."""
        if not self.blend[1]:
            return 0
        rated = {i for i, _ in self.rating_training} if self.blend[0] else set()
        return sum(1 for i, _ in self.play_training if i not in rated)


def _spread(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _correlation(xs: list[float], ys: list[float]) -> float:
    sx, sy = _spread(xs), _spread(ys)
    if not sx or not sy:
        return 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) * sx * sy)


def _scaled(predicted: list[float], targets: list[float]) -> float:
    """The number that turns predictions into the scale of their targets: as spread out as the
    targets, times how well they follow them (a weak predictor is held toward your usual liking)."""
    if len(targets) < MIN_GAMES_TO_LEARN or not _spread(targets):
        return 1.0  # too few games to tell: as learned
    correlation = max(_correlation(predicted, targets), CORRELATION_FLOOR)
    return correlation * _spread(targets) / max(_spread(predicted), SPREAD_FLOOR)


def blend(
    mode: str,
    rated: list[tuple[float, float, float]],
    played: list[tuple[float, float]],
) -> tuple[float, float]:
    """How much the taste from ratings and the taste from play count: (from ratings, from play).

    `rated` is (taste from ratings, taste from play, your rating) for each rated game and `played`
    (taste from play, your play) for each played game, every prediction made without that game.

    "Your ratings only" and "How you play only" use one taste, put on the scale of what it learned
    from. "Mix of both" puts both on the scale of your ratings and shares between them by how well
    each predicts your ratings (its correlation with them, never below zero), starting from
    BLEND_START as if BLEND_PRIOR rated games had said so. With no ratings, the mix is play alone;
    with no play, ratings alone.
    """
    ratings_only = (_scaled([r for r, _, _ in rated], [y for _, _, y in rated]), 0.0)
    play_only = (0.0, _scaled([p for p, _ in played], [y for _, y in played]))
    if mode == "ratings" or (mode == "mix" and not played):
        return ratings_only if rated else (1.0, 0.0)
    if mode == "engagement" or not rated:
        return play_only if played else (0.0, 1.0)

    from_r = [r for r, _, _ in rated]
    from_p = [p for _, p, _ in rated]
    yours = [y for _, _, y in rated]
    n = len(rated) if len(rated) >= MIN_GAMES_TO_LEARN else 0  # too few games to tell: the starting shares
    share_r = n * max(_correlation(from_r, yours), 0.0) + BLEND_PRIOR * BLEND_START[0]
    share_p = n * max(_correlation(from_p, yours), 0.0) + BLEND_PRIOR * BLEND_START[1]
    share = share_r / (share_r + share_p)
    # Each taste as spread out as the other, then shared, then put on the scale of your ratings.
    spread_r, spread_p = max(_spread(from_r), SPREAD_FLOOR), max(_spread(from_p), SPREAD_FLOOR)
    combined = [share * r / spread_r + (1 - share) * p / spread_p for r, p in zip(from_r, from_p)]
    scale = _scaled(combined, yours)
    return (scale * share / spread_r, scale * (1 - share) / spread_p)


@dataclass(frozen=True)
class Scores:
    """Recommendation scores for every game with tags, plus which ones count as
    recommended (unplayed, not dismissed, not a duplicate edition), by list."""

    by_id: dict[int, Recommendation]
    backlog: set[int]
    family: set[int]
    discover: set[int]


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


def play_scale(games: list[Game]) -> PlayScale:
    """Percentiles of your play among the games you have played."""
    return PlayScale(g.library_entry for g in games if is_played(g.library_entry, bool(g.family_shared)))


def play_score_of(session: Session, app_id: int) -> PlayScore | None:
    """How much your play of one game says you liked it (None when you have not played it)."""
    games = list(session.scalars(select(Game).join(LibraryEntry).where(Game.type == "game")))
    game = next((g for g in games if g.steam_app_id == app_id), None)
    if game is None or not is_played(game.library_entry, bool(game.family_shared)):
        return None
    return play_scale(games).score(game.library_entry)


def _build_model(
    session: Session, ratings: dict[int, float], not_interested: dict[int, str], mode: str | None = None
) -> _Model:
    mode = mode or get_taste_mode(session)
    # Favorites and "not interested" stand in for ratings in the mix; in every mode they keep a
    # game out of the candidates.
    stand_ins = {app_id: NOT_INTERESTED_RATING for app_id in not_interested}
    stand_ins.update(favorite_ratings(session))
    tracked = tracked_ids(session)

    games = list(session.scalars(select(Game).where(Game.type == "game")))
    # Rarity is measured on your own library, not on the pool picked by tag.
    idf = compute_idf(g.tags for g in games if g.library_entry is not None)
    vectors = {g.steam_app_id: tag_vector(g.tags, idf) for g in games}
    scale = play_scale(games)

    model = _Model(vectors, games, mode, session=session, shown_ratings=dict(ratings), hide_adult=get_hide_adult(session))
    for g in games:
        app_id, entry, family = g.steam_app_id, g.library_entry, bool(g.family_shared)
        model.games_by_id[app_id] = g
        if entry is not None:
            (model.family_titles if family else model.owned_titles).add(_base_name(g.name))
        rating = ratings.get(app_id)
        played = is_played(entry, family)
        if rating is None and app_id not in stand_ins and not played and app_id not in tracked:
            if vectors[app_id]:
                model.candidates.append(g)
            continue
        model.played_titles.add(_base_name(g.name))
        if not vectors[app_id]:
            continue
        counted = rating if (rating is not None or mode == "ratings") else stand_ins.get(app_id)
        if counted is not None:
            model.rating_training.append((app_id, rating_weight(counted)))
        if played:
            score = scale.score(entry)
            if score is not None:
                model.play_scores[app_id] = score
                model.play_training.append((app_id, score.weight))
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
    `app_ids`, predicted with everything known about that game left out of both tastes (its
    rating, favorite and play) and out of how the mix weighs them. Games without tags are skipped.
    (Each game's play is still among the percentiles the others are ranked by; that is minor.)"""
    model = _build_model(session, ratings, not_interested, mode)
    known: dict[tuple[int, int], float] = {}

    def kernel(a: int, b: int) -> float:
        key = (a, b) if a <= b else (b, a)
        if key not in known:
            known[key] = likeness(
                model.tag_units[a], model.text_units.get(a), model.tag_units[b], model.text_units.get(b), model.beta
            )
        return known[key]

    def without(training: list[tuple[int, float]], app_id: int) -> float:
        rest = [(i, w) for i, w in training if i != app_id]
        taste = fit(rest, model.tag_units, model.text_units, model.beta, kernel=kernel)
        return taste.predict(model.tag_units[app_id], model.text_units.get(app_id))

    out = {}
    for app_id in app_ids:
        if not model.vectors.get(app_id):
            continue
        by_ratings, by_play = blend(model.mode, model.rated_pairs(skip=app_id), model.played_pairs(skip=app_id))
        value = 0.0
        if by_ratings:
            value += by_ratings * without(model.rating_training, app_id)
        if by_play:
            value += by_play * without(model.play_training, app_id)
        out[app_id] = value
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
        if base in model.played_titles or (model.hide_adult and is_adult(cand.tags)):
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
