"""Your taste, learned from the games you rated or played.

The old way averaged every game you liked into one tag profile and compared each game to
that average, which waters down any single favourite: a game very close to Yakuza 0 was
compared with the blend of all your games. This way learns from each game instead. Every
game is described by its tags (how relevant and how rare) and by the words of its "About
this game" text; two games are as alike as those descriptions are (half tags, half words).
A ridge regression then learns, from how much you liked each game you played, which kinds
of games you like, and predicts that for every other game.

It is the same idea as a taste profile, just fitted to your own verdicts, so it gets better
every time you rate a game. Pure Python, no libraries: fitting is a few hundred games, and
the result is kept until your ratings or the games change.
"""

import math
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shelflife.models import Game

TEXT_WEIGHT = 0.5  # tags and the store text count equally; 0 with no text at all
RIDGE = 3.0  # how hard the fit is held back (more: smoother, less carried away by one game)
MATCH_SCALE = 4.0  # the fit holds predictions back toward zero; this stretches them so a close match reads as one
INTERCEPT_SHRINK = 5  # your average liking counts as this many extra neutral games: with a handful of games it is mostly zero

_STOP = frozenset(
    """the and for with that this you your are from have has their they his her its into over out can will all
    more than but not one two new game games player players take back each every other about when where which who
    what through while across along within without between among make makes made world story play played playing
    get gets also only just like most some such then them these those there here been being were was had does did
    doing""".split()
)
_WORD = re.compile(r"[a-z][a-z'-]{2,}")

Vector = dict[str, float]


def unit(vec: Vector) -> Vector:
    norm = math.sqrt(sum(x * x for x in vec.values()))
    return {k: x / norm for k, x in vec.items()} if norm else {}


def _dot(a: Vector, b: Vector) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b[k] for k, x in a.items() if k in b)


def unit_tag_vectors(vectors: dict[int, Vector]) -> dict[int, Vector]:
    return {app_id: unit(v) for app_id, v in vectors.items()}


# ---- the words of each game's store text ----


def text_vectors(texts: dict[int, str]) -> dict[int, Vector]:
    """A unit-length word vector for each text: how often a word is used (damped) times how
    rare it is across the games. Words used by only one game say nothing about likeness."""
    tokens = {i: [w for w in _WORD.findall(t.lower()) if w not in _STOP] for i, t in texts.items()}
    df: dict[str, int] = defaultdict(int)
    for words in tokens.values():
        for w in set(words):
            df[w] += 1
    n = len(tokens)
    out: dict[int, Vector] = {}
    for i, words in tokens.items():
        tf: dict[str, int] = defaultdict(int)
        for w in words:
            tf[w] += 1
        out[i] = unit({w: (1 + math.log(c)) * math.log(n / df[w]) for w, c in tf.items() if df[w] >= 2})
    return out


_text_lock = threading.Lock()
_text_cache: dict = {"key": None, "vectors": {}}


def text_index(session: Session) -> dict[int, Vector]:
    """Word vectors for every game with store text, rebuilt only when the texts change."""
    key = session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(func.length(Game.about_text)), 0),
            func.coalesce(func.sum(Game.steam_app_id), 0),
        ).where(
            Game.type == "game", Game.about_text.is_not(None), Game.about_text != ""
        )
    ).one()
    key = tuple(key)
    with _text_lock:
        if _text_cache["key"] != key or key[0] == 0:
            rows = session.execute(
                select(Game.steam_app_id, Game.about_text).where(
                    Game.type == "game", Game.about_text.is_not(None), Game.about_text != ""
                )
            ).all()
            _text_cache["vectors"] = text_vectors({i: t for i, t in rows})
            _text_cache["key"] = key
        return _text_cache["vectors"]


# ---- the fit ----


@dataclass
class LearnedTaste:
    """What was learned. `predict` is how much more (or less) you would like a game than
    your usual game, on the scale of the weights it was fitted to (-1 to +1): 0 for a game
    unlike any you have a verdict on, since nothing is known about it."""

    beta: float = 0.0  # share of the text in likeness
    mean: float = 0.0  # your usual liking, which the fit is centred on
    tag_part: Vector = field(default_factory=dict)  # sum of (learned weight x unit tag vector)
    text_part: Vector = field(default_factory=dict)
    trained_on: int = 0

    def predict(self, tag_unit: Vector, text_unit: Vector | None) -> float:
        value = (1 - self.beta) * _dot(tag_unit, self.tag_part)
        if self.beta and text_unit:
            value += self.beta * _dot(text_unit, self.text_part)
        return value


def likeness(a_tags: Vector, a_text: Vector | None, b_tags: Vector, b_text: Vector | None, beta: float) -> float:
    """How alike two games are, 0 to 1, from unit tag vectors and unit word vectors."""
    value = (1 - beta) * _dot(a_tags, b_tags)
    if beta and a_text and b_text:
        value += beta * _dot(a_text, b_text)
    return value


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve a symmetric positive definite system (Cholesky)."""
    n = len(matrix)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        row_i = low[i]
        for j in range(i + 1):
            row_j = low[j]
            s = matrix[i][j] - sum(x * y for x, y in zip(row_i[:j], row_j[:j]))
            row_i[j] = math.sqrt(max(s, 1e-12)) if i == j else s / row_j[j]
    z = [0.0] * n
    for i in range(n):
        z[i] = (rhs[i] - sum(x * y for x, y in zip(low[i][:i], z[:i]))) / low[i][i]
    out = [0.0] * n
    for i in reversed(range(n)):
        out[i] = (z[i] - sum(low[k][i] * out[k] for k in range(i + 1, n))) / low[i][i]
    return out


def fit(
    training: list[tuple[int, float]],
    tag_units: dict[int, Vector],
    text_units: dict[int, Vector],
    beta: float,
    ridge: float = RIDGE,
) -> LearnedTaste:
    """Learn from `training`: (game id, how much you liked it, -1 to 1)."""
    n = len(training)
    if not n:
        return LearnedTaste(beta=beta)
    ids = [i for i, _ in training]
    targets = [w for _, w in training]
    mean = sum(targets) / (n + INTERCEPT_SHRINK)
    centered = [w - mean for w in targets]
    gram = [[0.0] * n for _ in range(n)]
    for a in range(n):
        for b in range(a, n):
            k = likeness(tag_units[ids[a]], text_units.get(ids[a]), tag_units[ids[b]], text_units.get(ids[b]), beta)
            gram[a][b] = gram[b][a] = k + (ridge if a == b else 0.0)
    alpha = _solve(gram, centered)
    tag_part: dict[str, float] = defaultdict(float)
    text_part: dict[str, float] = defaultdict(float)
    for i, a in zip(ids, alpha):
        for t, x in tag_units[i].items():
            tag_part[t] += a * x
        for w, x in text_units.get(i, {}).items():
            text_part[w] += a * x
    return LearnedTaste(beta=beta, mean=mean, tag_part=dict(tag_part), text_part=dict(text_part), trained_on=n)


_fit_lock = threading.Lock()
_fit_cache: dict = {"key": None, "value": None}


def fit_cached(
    key: tuple,
    training: list[tuple[int, float]],
    tag_units: dict[int, Vector],
    text_units: dict[int, Vector],
    beta: float,
) -> LearnedTaste:
    """`fit`, kept until `key` (your verdicts and the games they were learned from) changes."""
    with _fit_lock:
        if _fit_cache["key"] != key:
            _fit_cache["value"] = fit(training, tag_units, text_units, beta)
            _fit_cache["key"] = key
        return _fit_cache["value"]
