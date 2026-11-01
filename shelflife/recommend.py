"""Recommendations from how well a game fits what you liked, scaled by review quality.

What teaches your taste, and nothing else (the taste mode on My taste picks between the first two):
- **Your star ratings** (modes "both" and "ratings"). A rating of 3 stars or more says how much you liked a game, `(rating - 3) / 2`
  (0 to +1), and teaches the broad taste: games like it rise. A rating under 3 says "not this
  game": it pushes down only games very like it (a sequel, a near-clone), through a narrow
  likeness (`LOOK_ALIKE_POWER`), never the whole genre. Disliking one horror game says nothing
  about horror.
- **Games you played but didn't rate** (modes "both", the default, and "play"). Playing a game
  says you probably liked it; how much you played it (shelflife/play.py: against your other games
  and the game's own players) says how sure that is. So such a game counts as the rating your
  played games usually get, with a confidence that grows with your play. Little play means "not
  sure", never "didn't like it". In "play" every played game counts this way, rated or not, and
  "usually get" is a plain like (+0.5), since ratings are left out.
- **"Not interested"** (every mode) hides a game, and counts as a weak "not this game": like a 2-star rating,
  but less sure (`NOT_INTERESTED_CONFIDENCE`), since it can also mean "not now" or "too expensive".
  So it pushes down only its close look-alikes, a little less than a 2-star rating would. A
  rating you gave the game wins over it.

Favorites, statuses and the wishlist are diary records and never change your taste. (Research
behind this: PLANS.md, section 8b.)

shelflife/learned.py does the learning: it predicts, from a game's tags and the words of its store
text, how much you would like it, fitted to your games one by one. The tag profile (your liked
games' tags) is kept for the My taste page, for the tags a pick matches and for seeding searches.

Candidates come in four lists: games you own but have barely played (backlog), games you can play
through Steam Family Sharing (family), your wishlist, and well-reviewed games you don't have yet
(discover). Each is scored by the prediction times a quality factor from Steam reviews.
"""

import re
import threading
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from functools import cached_property

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from shelflife.db import SessionLocal
from shelflife.diary import favorite_ids, tracked_ids
from shelflife.diary import load_not_interested as diary_not_interested
from shelflife.diary import load_ratings as diary_ratings
from shelflife.learned import (
    MATCH_SCALE,
    RIDGE,
    TEXT_WEIGHT,
    LearnedTaste,
    Vector,
    _solve,
    fit,
    fit_cached,
    likeness,
    text_index,
    unit_tag_vectors,
)
from shelflife.models import Game
from shelflife.play import BACKLOG_MAX_MIN, PlayScale, PlayScore, is_played
from shelflife.quality import quality_factor
from shelflife.settings import get_hide_adult, get_taste_mode
from shelflife.tags import compute_idf, is_adult, tag_vector

__all__ = ["BACKLOG_MAX_MIN", "is_played"]  # also imported from here by other modules

NEUTRAL_RATING = 3.0  # 3 stars: an ordinary game for you; under it, a dislike
# A dislike reaches only games very like the disliked one: likeness (0 to 1) to this power, so a
# sequel at 0.7 feels half of it (0.34) and a game of the same genre at 0.3 almost nothing (0.03).
# Measured on the owner's games: ratings +0.438 -> +0.452, rankings +0.317 -> +0.387 (PLANS.md 8).
LOOK_ALIKE_POWER = 3
# How sure a game you only played makes the taste: its ridge is RIDGE / (PLAY_CONFIDENCE x play
# score), so a game you played far more than usual (score near 1) counts about like a rating and
# one you barely played (score 0.1) about a tenth. Tried 0.1 to 1 on the owner's games; 1 did best.
PLAY_CONFIDENCE = 1.0
PLAY_SCORE_FLOOR = 0.05  # the least a played game counts, so it is never infinitely unsure
WATCH_LIKENESS = 0.6  # a pick this alike to a game you rated low (sequel-close) gets a heads-up naming it


def rating_weight(rating: float) -> float:
    """A star rating as -1 (1 star) to +1 (5 stars); 3 stars is 0."""
    return (rating - NEUTRAL_RATING) / (5.0 - NEUTRAL_RATING)


# A game you marked "not interested" (and didn't rate) counts as a 2-star rating held half as
# sure: its ridge is RIDGE / NOT_INTERESTED_CONFIDENCE, so alone it pushes its look-alikes down a
# bit over half as far as a 2-star rating does. Not measurable on the owner's games (none of the
# marked games are rated); chosen to stay under a real rating.
NOT_INTERESTED_WEIGHT = rating_weight(2.0)
NOT_INTERESTED_CONFIDENCE = 0.5


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
    similarity: float  # predicted liking, stretched for display: how well the game fits you
    quality: float
    because: Sequence[str]  # games you liked that pulled this one up
    tags: list[str]  # tags of this game that match your taste
    watch: list[str]  # games you rated low or aren't interested in that this one closely resembles


@dataclass(frozen=True)
class Taste:
    profile: dict[str, float]
    explicit_count: int  # games learned from your ratings
    implicit_count: int  # games learned from your play alone
    disliked: list[tuple[Game, float]] = field(default_factory=list)  # (game, your rating), under 3 stars
    dismissed: list[Game] = field(default_factory=list)  # marked "not interested", not rated


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
    session: Session | None = None
    mode: str = "both"  # what teaches the taste: "both", "ratings" or "play" (settings.TASTE_MODES)
    hide_adult: bool = False  # leave games for adults out of the lists
    candidates: list[Game] = field(default_factory=list)
    played_titles: set[str] = field(default_factory=set)
    owned_titles: set[str] = field(default_factory=set)
    family_titles: set[str] = field(default_factory=set)
    games_by_id: dict[int, Game] = field(default_factory=dict)
    shown_ratings: dict[int, float] = field(default_factory=dict)  # your ratings, for the reasons on a card
    scale: PlayScale | None = None  # percentiles of your play, for a game page
    likes: dict[int, float] = field(default_factory=dict)  # rated 3 stars or more: (rating - 3) / 2
    dislikes: dict[int, float] = field(default_factory=dict)  # rated under 3 stars
    dismissed: set[int] = field(default_factory=set)  # marked "not interested", not rated
    played: dict[int, PlayScore] = field(default_factory=dict)  # played, not rated (or any played game in "play"); learned from unless the mode is "ratings"
    rated_and_played: dict[int, float] = field(default_factory=dict)  # what the games you played and rated got

    @property
    def learns_from_play(self) -> bool:
        return self.mode != "ratings"

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

    def kernel(self, a: int, b: int) -> float:
        return likeness(self.tag_units[a], self.text_units.get(a), self.tag_units[b], self.text_units.get(b), self.beta)

    def usual_rating(self, skip: int | None = None) -> float:
        """What a game you played usually turns out to be, as a rating weight (never below 0: playing
        a game is never taken as disliking it). One half until you have rated games you played."""
        got = [r for i, r in self.rated_and_played.items() if i != skip]
        return max(sum(got) / len(got), 0.0) if got else 0.5

    def confidence(self, app_id: int) -> float:
        """How sure a game you only played makes the taste, 0 to 1."""
        return min(PLAY_CONFIDENCE * max(self.played[app_id].value, PLAY_SCORE_FLOOR), 1.0)

    def broad_training(self, skip: int | None = None) -> tuple[list[tuple[int, float]], dict[int, float]]:
        """(game id, how much you liked it) for every game the broad taste learns from, and the
        ridge of each game you only played (the less sure, the larger)."""
        training = [(i, r) for i, r in self.likes.items() if i != skip]
        ridges: dict[int, float] = {}
        if self.learns_from_play:
            usual = self.usual_rating(skip)
            for i in self.played:
                if i != skip:
                    training.append((i, usual))
                    ridges[i] = RIDGE / self.confidence(i)
        return training, ridges

    @cached_property
    def broad(self) -> LearnedTaste:
        training, ridges = self.broad_training()
        training.sort()
        key = (
            tuple(training),
            tuple((i, tuple(sorted(self.tag_units[i].items()))) for i, _ in training),
            self.beta,
            len(self.text_units),
            sum(len(v) for v in self.text_units.values()),
        )
        return fit_cached(key, training, self.tag_units, self.text_units, self.beta, ridges=ridges)

    def narrow_alphas(self, skip: int | None = None) -> list[tuple[int, float]]:
        """The dislikes and the games you're not interested in, as (game id, weight) on the narrow
        likeness."""
        targets = {i: (NOT_INTERESTED_WEIGHT, RIDGE / NOT_INTERESTED_CONFIDENCE) for i in self.dismissed}
        targets |= {i: (w, RIDGE) for i, w in self.dislikes.items()}
        ids = [i for i in targets if i != skip]
        if not ids:
            return []
        gram = [[self.kernel(a, b) ** LOOK_ALIKE_POWER + (targets[a][1] if x == y else 0.0) for y, b in enumerate(ids)] for x, a in enumerate(ids)]
        return list(zip(ids, _solve(gram, [targets[i][0] for i in ids])))

    @cached_property
    def narrow(self) -> list[tuple[int, float]]:
        return self.narrow_alphas()

    def push_down(self, app_id: int, narrow: list[tuple[int, float]]) -> float:
        return sum(a * self.kernel(app_id, i) ** LOOK_ALIKE_POWER for i, a in narrow if i != app_id)

    def liking(self, app_id: int) -> float:
        """How much more (or less) than your usual game you would like this one, on the scale of a
        rating (+1 = two stars above your usual); 0 when it has no tags or nothing is known."""
        if not self.vectors.get(app_id):
            return 0.0
        value = self.broad.predict(self.tag_units[app_id], self.text_units.get(app_id))
        return value + self.push_down(app_id, self.narrow)

    @cached_property
    def weights(self) -> dict[int, float]:
        """How much each liked game shapes the tag profile: its rating, or for a game you only
        played, the usual rating times how sure its play makes us."""
        out = {i: r for i, r in self.likes.items()}
        if self.learns_from_play:
            usual = self.usual_rating()
            out.update({i: usual * self.confidence(i) for i in self.played})
        return out

    @cached_property
    def contributors(self) -> list[tuple[float, Game, float | None]]:
        return [(w, self.games_by_id[i], self.shown_ratings.get(i)) for i, w in self.weights.items() if w > 0]

    @cached_property
    def profile(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for app_id, w in self.weights.items():
            for tag, value in self.vectors[app_id].items():
                out[tag] = out.get(tag, 0.0) + w * value
        return out

    @property
    def explicit(self) -> int:
        return len(self.likes) + len(self.dislikes)

    @property
    def implicit(self) -> int:
        return len(self.played) if self.learns_from_play else 0

    @cached_property
    def disliked(self) -> list[tuple[Game, float]]:
        return sorted(((self.games_by_id[i], self.shown_ratings[i]) for i in self.dislikes), key=lambda gr: (gr[1], gr[0].name))

    def why_low(self, app_id: int) -> str:
        """How you said "not this game", for a heads-up."""
        return f"rated {self.shown_ratings[app_id]:g}" if app_id in self.dislikes else "not interested"

    @cached_property
    def dismissed_games(self) -> list[Game]:
        return sorted((self.games_by_id[i] for i in self.dismissed), key=lambda g: g.name.lower())

    def taste(self) -> Taste:
        return Taste(self.profile, self.explicit, self.implicit, self.disliked, self.dismissed_games)


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


def _build_model(
    session: Session, ratings: dict[int, float], not_interested: dict[int, str], mode: str | None = None
) -> _Model:
    mode = get_taste_mode(session) if mode is None else mode
    # In "play" your ratings teach nothing, and the reasons on a card show hours, not stars.
    taught = ratings if mode != "play" else {}
    # In your diary, so not recommended. Of these only "not interested" teaches anything (below).
    in_diary = set(not_interested) | favorite_ids(session) | tracked_ids(session)

    games = list(session.scalars(select(Game).where(Game.type == "game").options(selectinload(Game.library_entry))))
    # Rarity is measured on your own library, not on the pool picked by tag.
    idf = compute_idf(g.tags for g in games if g.library_entry is not None)
    vectors = {g.steam_app_id: tag_vector(g.tags, idf) for g in games}
    scale = play_scale(games)

    model = _Model(
        vectors, games, session=session, mode=mode, hide_adult=get_hide_adult(session),
        shown_ratings=dict(taught), scale=scale,
    )
    for g in games:
        app_id, entry, family = g.steam_app_id, g.library_entry, bool(g.family_shared)
        model.games_by_id[app_id] = g
        if entry is not None:
            (model.family_titles if family else model.owned_titles).add(_base_name(g.name))
        rating = ratings.get(app_id)
        played = is_played(entry, family)
        if rating is None and not played and app_id not in in_diary:
            if vectors[app_id]:
                model.candidates.append(g)
            continue
        model.played_titles.add(_base_name(g.name))
        if not vectors[app_id]:
            continue
        rating = taught.get(app_id)
        if rating is not None:
            weight = rating_weight(rating)
            (model.likes if weight >= 0 else model.dislikes)[app_id] = weight
            if played:
                model.rated_and_played[app_id] = weight
        elif app_id in not_interested:  # over play: you played it, then said you're not interested
            model.dismissed.add(app_id)
        elif played:
            score = scale.score(entry)
            if score is not None:
                model.played[app_id] = score
    return model


# ---- kept between pages until your data changes ----
#
# Building the model reads every game and scoring reads it again; with a few thousand games that
# took most of a second on every page, even when nothing had changed. So the model and the scores
# are kept, and a cheap fingerprint of the data (counts, sums and the latest change times of
# everything the model reads, one query) says when they must be built again: a rating, a sync, a
# search for new games or a changed setting changes it.

_DATA_VERSION = text("""
SELECT
  (SELECT count(*) || ':' || coalesce(sum(length(tags)), 0) || ':' || coalesce(sum(length(type)), 0) || ':'
          || coalesce(sum(family_shared), 0) || ':' || coalesce(sum(reviews_positive), 0) || ':'
          || coalesce(sum(reviews_negative), 0) || ':' || coalesce(max(details_fetched_at), '') || ':'
          || coalesce(max(tags_fetched_at), '') || ':' || coalesce(sum(length(player_hours)), 0) || ':'
          || coalesce(sum(player_hours_sample), 0) || ':' || coalesce(sum(length(about_text)), 0)
     FROM games),
  (SELECT count(*) || ':' || coalesce(sum(playtime_total_min), 0) || ':' || coalesce(sum(playtime_recent_min), 0) || ':'
          || coalesce(sum(achievements_unlocked), 0) || ':' || coalesce(sum(achievements_total), 0) || ':'
          || coalesce(sum(rarest_unlocked_pct), 0) || ':' || coalesce(max(last_played), '') || ':'
          || coalesce(sum(length(unlock_times)), 0)
     FROM library_entries),
  (SELECT count(*) || ':' || coalesce(max(updated_at), '') || ':' || coalesce(sum(rating), 0) || ':'
          || coalesce(sum(not_interested), 0) || ':' || coalesce(sum(favorite), 0) || ':' || coalesce(sum(length(status)), 0)
     FROM diary_entries),
  (SELECT coalesce(group_concat(key || '=' || value, ';'), '') FROM settings)
""")

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "model": None, "scores": None}


def data_version(session: Session) -> tuple:
    """A fingerprint of everything the model is built from."""
    return tuple(session.execute(_DATA_VERSION).one())


def _cached(session: Session, ratings: dict[int, float], not_interested: dict[int, str]) -> tuple[_Model, Scores]:
    """The model and every game's score, built in a session of their own (so they outlive this
    page's) and kept until the data, the ratings asked about or the setting change."""
    key = (data_version(session), tuple(sorted(ratings.items())), tuple(sorted(not_interested)))
    with _cache_lock:
        if _cache["key"] != key:
            with SessionLocal() as own:
                model = _build_model(own, ratings, not_interested)
                model.broad, model.narrow, model.profile, model.disliked, model.dismissed_games  # noqa: B018  (worked out while the session is open)
                scores = _score_everything(model)
                model.session = None
            _cache.update(key=key, model=model, scores=scores)
        return _cache["model"], _cache["scores"]


def clear_cache() -> None:
    with _cache_lock:
        _cache.update(key=None, model=None, scores=None)


def _score_everything(model: _Model) -> Scores:
    backlog, family, discover = _classify(model)
    return Scores(
        by_id={g.steam_app_id: _score(g, model) for g in model.games if model.vectors[g.steam_app_id]},
        backlog={g.steam_app_id for g in backlog},
        family={g.steam_app_id for g in family},
        discover={g.steam_app_id for g in discover},
    )


def play_score_of(session: Session, app_id: int) -> PlayScore | None:
    """How much your play of one game says you liked it (None when you have not played it)."""
    game = session.get(Game, app_id)
    if game is None or not is_played(game.library_entry, bool(game.family_shared)):
        return None
    model, _ = _cached(session, diary_ratings(session), diary_not_interested(session))
    return model.scale.score(game.library_entry)


def top_taste_tags(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str],
    n: int = 8,
) -> list[str]:
    """The tags your taste profile leans toward most; seeds the discovery pool."""
    profile = _cached(session, ratings, not_interested)[0].profile
    ranked = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, v in ranked[:n] if v > 0]


def taste_profile(
    session: Session, ratings: dict[int, float], not_interested: dict[int, str]
) -> Taste:
    model, _ = _cached(session, ratings, not_interested)
    return model.taste()


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
    _, scores = _cached(session, ratings, not_interested)
    return scores.by_id.get(app_id)


def held_out_liking(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str],
    app_ids: Collection[int],
    mode: str | None = None,
) -> dict[int, float]:
    """For checking the model (tools/evaluate.py): how much you would like each game in `app_ids`,
    predicted with everything known about that game left out (its rating and its play, also from
    "what a game you played usually gets"). Games without tags are skipped. (Each game's play is
    still among the percentiles the others are ranked by; that is minor.)"""
    model = _build_model(session, ratings, not_interested, mode)
    known: dict[tuple[int, int], float] = {}
    worked_out = model.kernel

    def kernel(a: int, b: int) -> float:
        key = (a, b) if a <= b else (b, a)
        if key not in known:
            known[key] = worked_out(a, b)
        return known[key]

    model.kernel = kernel  # the same likeness, remembered across the many refits
    out = {}
    for app_id in app_ids:
        if not model.vectors.get(app_id):
            continue
        training, ridges = model.broad_training(skip=app_id)
        taste = fit(training, model.tag_units, model.text_units, model.beta, kernel=kernel, ridges=ridges)
        value = taste.predict(model.tag_units[app_id], model.text_units.get(app_id))
        out[app_id] = value + model.push_down(app_id, model.narrow_alphas(skip=app_id))
    return out


def _because(cand: Game, model: _Model) -> list[str]:
    """The two games you liked that this one is closest to."""
    pulls = sorted(
        (
            (sim * w, sim, g, r)
            for w, g, r in model.contributors
            if g.steam_app_id != cand.steam_app_id  # not compared with itself
            for sim in [model.kernel(cand.steam_app_id, g.steam_app_id)]
        ),
        key=lambda p: p[0],
        reverse=True,
    )[:2]
    return [_label(g, r, sim) for c, sim, g, r in pulls if c > 0]


def _score(cand: Game, model: _Model) -> Recommendation:
    app_id = cand.steam_app_id
    vec = model.vectors[app_id]
    # Below 0: it closely resembles a game you rated low or aren't interested in, or is unlike your usual games.
    similarity = min(model.liking(app_id) * MATCH_SCALE, 1.0)
    by_tag = sorted(((model.profile.get(t, 0.0) * v, t) for t, v in vec.items()), reverse=True)
    near_dislikes = sorted(
        ((model.kernel(app_id, i), i) for i in (*model.dislikes, *model.dismissed) if i != app_id), reverse=True
    )[:1]
    quality = quality_factor(cand.reviews_positive, cand.reviews_negative)
    return Recommendation(
        game=cand,
        score=similarity * quality,
        similarity=similarity,
        quality=quality,
        because=_Lazy(lambda: _because(cand, model)),
        tags=[t for c, t in by_tag[:3] if c > 0],
        watch=[
            f"{model.games_by_id[i].name} ({model.why_low(i)})"
            for sim, i in near_dislikes
            if sim >= WATCH_LIKENESS
        ],
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
    return _cached(session, ratings, not_interested or {})[1]


def recommend(
    session: Session,
    ratings: dict[int, float],
    not_interested: dict[int, str] | None = None,
    limit: int = 10,
    wishlist_ids: Collection[int] | None = None,
) -> Recommendations:
    """The recommendation lists. Games in `wishlist_ids` that you do not own come out of
    "discover" into their own list, so each game is listed once."""
    model, scores = _cached(session, ratings, not_interested or {})
    wished = set(wishlist_ids or ())
    pick = lambda ids: [scores.by_id[i] for i in ids]  # noqa: E731
    backlog = pick(scores.backlog)
    family = pick(scores.family)
    discover = pick(i for i in scores.discover if i not in wished)
    wishlist = pick(i for i in scores.discover if i in wished)

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
        taste=model.taste(),
        wishlist=top(wishlist),
    )
