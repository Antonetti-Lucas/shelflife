"""The held-out predictions tools/evaluate.py checks the model with: a game's own rating and play
must never help predict it."""

import pytest

from shelflife.learned import fit
from shelflife.recommend import _build_model, held_out_liking
from shelflife.settings import get_learn_from_play, set_learn_from_play
from tests.factories import add_game


@pytest.fixture
def library(session):
    """Two loved games alike (Zeta), one disliked (Eta), one loved game unlike any other (Omega)."""
    add_game(session, 1, "Zeta One", tags={"Zeta": 100, "Kappa": 40}, minutes=6000, achievements=(30, 30))
    add_game(session, 2, "Zeta Two", tags={"Zeta": 100, "Kappa": 30}, minutes=6000, achievements=(30, 30))
    add_game(session, 3, "Eta", tags={"Eta": 100}, minutes=120)
    add_game(session, 4, "Omega", tags={"Omega": 100}, minutes=60000, achievements=(30, 30))
    for i, tag in enumerate(["Puzzle", "Racing", "Farming"], start=10):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=90)
    return {1: 5.0, 2: 5.0, 3: 1.0, 4: 5.0}


@pytest.mark.parametrize("learn", [True, False])
def test_a_game_unlike_all_others_is_predicted_from_nothing_however_much_you_loved_and_played_it(session, library, learn):
    assert held_out_liking(session, library, {}, [4], learn_from_play=learn)[4] == pytest.approx(0.0)


@pytest.mark.parametrize("learn", [True, False])
def test_a_game_is_predicted_from_the_games_like_it(session, library, learn):
    assert held_out_liking(session, library, {}, [1], learn_from_play=learn)[1] > 0


def test_holding_out_a_rated_game_is_the_same_as_never_having_rated_it(session, library):
    held = held_out_liking(session, library, {}, [1], learn_from_play=False)[1]
    without = _build_model(session, {k: v for k, v in library.items() if k != 1}, {}, learn_from_play=False)
    training, ridges = without.broad_training()
    taste = fit(training, without.tag_units, without.text_units, without.beta, ridges=ridges)
    expected = taste.predict(without.tag_units[1], without.text_units.get(1)) + without.push_down(1, without.narrow_alphas())
    assert held == pytest.approx(expected)


def test_the_check_never_changes_your_choice(session, library):
    set_learn_from_play(session, False)
    held_out_liking(session, library, {}, [1, 2], learn_from_play=True)
    assert get_learn_from_play(session) is False


def test_games_without_tags_are_skipped(session, library):
    add_game(session, 5, "No tags", tags={}, minutes=600)
    assert set(held_out_liking(session, library, {}, [1, 5])) == {1}
