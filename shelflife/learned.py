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
from collections.abc import Callable
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
# Store-page boilerplate: about the product page, the keyboard or the languages, not about the game.
# Rare enough to look meaningful, so without this list two unrelated games can match on "esc" or "logo".
_BOILERPLATE = frozenset(
    """steam steamworks esc alt ctrl shift tab enter key keys keyboard mouse controller gamepad windows mac linux
    logo fonts font version versions update updates patch dlc dlcs edition editions bundle purchase purchased
    available includes include including included content contents feature features official copyright
    trademark trademarks reserved rights inc ltd llc corp studio studios publisher developer developers
    english chinese japanese korean russian german french spanish language languages subtitles translation
    simplified traditional support supports supported store page click link visit follow twitter discord
    facebook youtube website www http https com net org early access release released launch now
    please note warning""".split()
)
_WORD = re.compile(r"[a-z][a-z'-]{2,}")
MIN_GAMES_PER_WORD = 3  # a word only one or two games use says little about likeness
MAX_SHARE_PER_WORD = 0.15  # a word in more than this share of games is too common to tell games apart
MIN_TEXT_WORDS = 20  # fewer different words than this left (a text in another script, a two-line blurb): no text
LARGE_LIBRARY = 100  # the share and the minimums are about many texts; a handful of games keeps every shared word

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
    rare it is across the games.

    Only words that can tell games apart are kept: not common English, not store boilerplate,
    used by at least MIN_GAMES_PER_WORD games and by at most MAX_SHARE_PER_WORD of them. A text
    with fewer than MIN_TEXT_WORDS such words gets no vector (the game is compared on its tags
    alone): what is left of a description in Chinese or Japanese is "steam", "esc" and "logo",
    and a vector of three junk words matches anything that shares one of them. With fewer than
    LARGE_LIBRARY texts the share and the minimums are not applied (too few games to judge)."""
    tokens = {i: [w for w in _WORD.findall(t.lower()) if w not in _STOP and w not in _BOILERPLATE] for i, t in texts.items()}
    df: dict[str, int] = defaultdict(int)
    for words in tokens.values():
        for w in set(words):
            df[w] += 1
    n = len(tokens)
    large = n >= LARGE_LIBRARY
    fewest, most = (MIN_GAMES_PER_WORD, MAX_SHARE_PER_WORD * n) if large else (2, n)
    out: dict[int, Vector] = {}
    for i, words in tokens.items():
        tf: dict[str, int] = defaultdict(int)
        for w in words:
            if fewest <= df[w] <= most:
                tf[w] += 1
        if large and len(tf) < MIN_TEXT_WORDS:
            out[i] = {}
            continue
        out[i] = unit({w: (1 + math.log(c)) * math.log(n / df[w]) for w, c in tf.items()})
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
    # When asked for: for each game it learned from, (what `predict` would say had that game been
    # left out, the weight it was learned with minus `mean`). Used to weigh one taste against another.
    held_out: dict[int, tuple[float, float]] = field(default_factory=dict)

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


def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
    """L with L x L-transposed = matrix, for a symmetric positive definite matrix."""
    n = len(matrix)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        row_i = low[i]
        for j in range(i + 1):
            row_j = low[j]
            s = matrix[i][j] - sum(x * y for x, y in zip(row_i[:j], row_j[:j]))
            row_i[j] = math.sqrt(max(s, 1e-12)) if i == j else s / row_j[j]
    return low


def _solve_factored(low: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(low)
    z = [0.0] * n
    for i in range(n):
        z[i] = (rhs[i] - sum(x * y for x, y in zip(low[i][:i], z[:i]))) / low[i][i]
    out = [0.0] * n
    for i in reversed(range(n)):
        out[i] = (z[i] - sum(low[k][i] * out[k] for k in range(i + 1, n))) / low[i][i]
    return out


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve a symmetric positive definite system (Cholesky)."""
    return _solve_factored(_cholesky(matrix), rhs)


def _inverse_diagonal(low: list[list[float]]) -> list[float]:
    """The diagonal of the inverse of L x L-transposed: sum over k of (L^-1)[k][i] squared."""
    n = len(low)
    diag = [0.0] * n
    for j in range(n):  # column j of L^-1, by forward substitution
        col = [0.0] * n
        col[j] = 1 / low[j][j]
        for i in range(j + 1, n):
            row = low[i]
            col[i] = -sum(row[k] * col[k] for k in range(j, i)) / row[i]
        diag[j] = sum(x * x for x in col[j:])
    return diag


def fit(
    training: list[tuple[int, float]],
    tag_units: dict[int, Vector],
    text_units: dict[int, Vector],
    beta: float,
    ridge: float = RIDGE,
    kernel: Callable[[int, int], float] | None = None,
    held_out: bool = False,
) -> LearnedTaste:
    """Learn from `training`: (game id, how much you liked it, -1 to 1). `kernel(a, b)` gives
    the likeness of two games when it is already known (the evaluation fits many times over the
    same games); by default it is worked out here. With `held_out`, also work out what each
    training game would have been predicted without it (the standard shortcut for ridge
    regression, exact apart from your usual liking, which is kept as it is)."""
    n = len(training)
    if not n:
        return LearnedTaste(beta=beta)
    ids = [i for i, _ in training]
    targets = [w for _, w in training]
    mean = sum(targets) / (n + INTERCEPT_SHRINK)
    centered = [w - mean for w in targets]
    kernel = kernel or (
        lambda a, b: likeness(tag_units[a], text_units.get(a), tag_units[b], text_units.get(b), beta)
    )
    gram = [[0.0] * n for _ in range(n)]
    for a in range(n):
        for b in range(a, n):
            gram[a][b] = gram[b][a] = kernel(ids[a], ids[b]) + (ridge if a == b else 0.0)
    low = _cholesky(gram)
    alpha = _solve_factored(low, centered)
    left_out = {}
    if held_out:  # without game i, its prediction is its target minus alpha_i / (inverse of gram)_ii
        for i, c, a, d in zip(ids, centered, alpha, _inverse_diagonal(low)):
            left_out[i] = (c - a / d, c)
    tag_part: dict[str, float] = defaultdict(float)
    text_part: dict[str, float] = defaultdict(float)
    for i, a in zip(ids, alpha):
        for t, x in tag_units[i].items():
            tag_part[t] += a * x
        for w, x in text_units.get(i, {}).items():
            text_part[w] += a * x
    return LearnedTaste(
        beta=beta, mean=mean, tag_part=dict(tag_part), text_part=dict(text_part), trained_on=n, held_out=left_out
    )


FIT_CACHE_SIZE = 4  # a few tastes at once: the mix keeps one from your ratings and one from your play
_fit_lock = threading.Lock()
_fit_cache: dict[tuple, LearnedTaste] = {}


def fit_cached(
    key: tuple,
    training: list[tuple[int, float]],
    tag_units: dict[int, Vector],
    text_units: dict[int, Vector],
    beta: float,
    held_out: bool = False,
) -> LearnedTaste:
    """`fit`, kept until `key` (your verdicts and the games they were learned from) changes."""
    key = (key, held_out)
    with _fit_lock:
        if key not in _fit_cache:
            if len(_fit_cache) >= FIT_CACHE_SIZE:
                del _fit_cache[next(iter(_fit_cache))]  # the oldest
            _fit_cache[key] = fit(training, tag_units, text_units, beta, held_out=held_out)
        return _fit_cache[key]
