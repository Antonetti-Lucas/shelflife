"""The recommender: what each taste learns from, candidate lists, and why a game is recommended."""

import pytest

from shelflife import diary
from shelflife.recommend import (
    BLEND_START,
    SPREAD_FLOOR,
    _spread,
    _base_name,
    _build_model,
    blend,
    explain,
    rating_weight,
    recommend,
    score_all,
    taste_profile,
)
from tests.factories import add_game, build_scenario

# ---- what each taste learns from ----


@pytest.mark.parametrize("rating, weight", [(5.0, 1.0), (4.0, 0.5), (3.0, 0.0), (2.0, -0.5), (1.0, -1.0)])
def test_a_rating_maps_to_a_weight_between_minus_one_and_one(rating, weight):
    assert rating_weight(rating) == pytest.approx(weight)


@pytest.fixture
def three_games(session):
    """One game played a great deal, one played a little, one rated but never played here."""
    add_game(session, 1, "Played a lot", tags={"Zeta": 100}, minutes=1000 * 60, achievements=(10, 10), rarest=2.0)
    add_game(session, 2, "Played a little", tags={"Eta": 100}, minutes=90)
    add_game(session, 3, "Played elsewhere", tags={"Theta": 100})
    add_game(session, 4, "Filler", tags={"Iota": 100}, minutes=600)


def test_ratings_only_learns_from_star_ratings_and_nothing_else(session, three_games):
    diary.set_favorite(session, 4, True)
    model = _build_model(session, {1: 2.0, 3: 4.0}, {2: "Played a little"}, mode="ratings")
    assert dict(model.rating_training) == {1: pytest.approx(-0.5), 3: pytest.approx(0.5)}  # no favorite, no "not interested"
    assert model.blend[1] == 0 and model.implicit == 0


def test_how_you_play_learns_from_play_alone_and_can_say_not_for_me(session, three_games):
    model = _build_model(session, {1: 1.0, 2: 5.0, 3: 5.0}, {}, mode="engagement")
    play = dict(model.play_training)
    assert set(play) == {1, 2, 4}  # the game played elsewhere has no play to learn from
    assert play[1] > 0 > play[2]  # a hated game played a lot, a loved one barely played: play decides
    assert play[2] >= -0.5  # play alone can't say "hated"
    assert model.blend[0] == 0 and model.explicit == 0


def test_the_mix_learns_a_game_you_rated_and_played_from_both(session, three_games):
    model = _build_model(session, {1: 4.0, 3: 5.0}, {}, mode="mix")
    assert {i for i, _ in model.rating_training} == {1, 3}
    assert {i for i, _ in model.play_training} == {1, 2, 4}  # game 1 teaches through its rating and its play
    assert (model.explicit, model.implicit) == (2, 2)


def test_in_the_mix_favorites_and_not_interested_stand_in_for_ratings(session, three_games):
    diary.set_favorite(session, 4, True)
    model = _build_model(session, {}, {2: "Played a little"}, mode="mix")
    assert dict(model.rating_training) == {4: pytest.approx(1.0), 2: pytest.approx(-0.5)}


def test_backlog_games_are_not_a_verdict(session):
    add_game(session, 1, "Tried for ten minutes", tags={"Zeta": 100}, minutes=10)
    model = _build_model(session, {}, {}, mode="engagement")
    assert model.play_training == [] and [g.steam_app_id for g in model.candidates] == [1]


# ---- how the mix weighs the two tastes ----

PATTERN = [(0.3, 0.2), (-0.2, 0.2), (0.4, -0.1), (-0.3, -0.3), (0.1, 0.25), (-0.1, -0.2)] * 10


def test_without_ratings_the_mix_is_play_alone_and_without_play_it_is_ratings_alone():
    played = [(p, p) for _, p in PATTERN]
    rated = [(r, p, r) for r, p in PATTERN]
    assert blend("mix", [], played)[0] == 0 and blend("mix", [], played)[1] > 0
    assert blend("mix", rated, [])[1] == 0 and blend("mix", rated, [])[0] > 0


def test_the_single_modes_use_one_taste():
    rated = [(r, p, r) for r, p in PATTERN]
    played = [(p, p) for _, p in PATTERN]
    assert blend("ratings", rated, played)[1] == 0
    assert blend("engagement", rated, played)[0] == 0


def share_of_ratings(weights, rated):
    """The ratings taste's share of the mix, undoing the spreads it was divided by."""
    by_r, by_p = weights
    spread_r = max(_spread([r for r, _, _ in rated]), SPREAD_FLOOR)
    spread_p = max(_spread([p for _, p, _ in rated]), SPREAD_FLOOR)
    return by_r * spread_r / (by_r * spread_r + by_p * spread_p)


def test_the_mix_leans_on_the_taste_that_predicts_your_ratings():
    played = [(p, p) for _, p in PATTERN]
    ratings_right = [(r, p, r * 2) for r, p in PATTERN]
    assert share_of_ratings(blend("mix", ratings_right, played), ratings_right) > 0.7
    play_right = [(r, p, p * 2) for r, p in PATTERN]
    assert share_of_ratings(blend("mix", play_right, played), play_right) < 0.3


def test_a_few_contradicting_ratings_never_switch_a_taste_off_or_count_it_backwards():
    rated = [(-0.1, 0.0, 1.0), (0.0, 0.0, -1.0), (0.26, 0.17, -0.44)]
    weights = blend("mix", rated, [(0.2, 0.75), (0.2, 0.75), (-0.05, -0.12)])
    assert weights[0] > 0 and weights[1] > 0
    assert share_of_ratings(weights, rated) == pytest.approx(BLEND_START[0])  # too few games to move the start


# ---- editions of the same game ----


@pytest.mark.parametrize(
    "a, b",
    [
        ("Yakuza Kiwami 2 (Legacy)", "Yakuza Kiwami 2"),
        ("Mark of the Ninja: Remastered", "Mark of the Ninja"),
        ("Batman: Arkham Asylum GOTY Edition", "Batman: Arkham Asylum Game of the Year Edition"),
        ("HITMAN™ 2", "Hitman 2"),
        ("Little Nightmares Enhanced Edition", "Little Nightmares"),
    ],
)
def test_editions_of_one_game_share_a_base_name(a, b):
    assert _base_name(a) == _base_name(b)


def test_sequels_are_different_games():
    assert _base_name("Hitman 2") != _base_name("Hitman")
    assert _base_name("Yakuza 0") != _base_name("Yakuza Kiwami")


# ---- the lists ----


def test_each_kind_of_candidate_lands_in_its_own_list_in_taste_order(session):
    ratings = build_scenario(session)
    result = recommend(session, ratings, limit=50)
    assert [r.game.name for r in result.backlog] == ["Backlog Stealth", "Backlog Sports"]
    assert [r.game.name for r in result.family] == ["Family Stealth"]
    assert [r.game.name for r in result.discover] == ["Pool Stealth Good", "Pool Stealth Bad"]


def test_an_edition_of_a_game_you_played_is_not_recommended(session):
    ratings = build_scenario(session)
    everything = recommend(session, ratings, limit=50)
    names = [r.game.name for r in everything.backlog + everything.family + everything.discover]
    assert "Stealth Hero Remastered" not in names


def test_a_liked_tag_scores_above_a_disliked_one(session):
    ratings = build_scenario(session)
    liked, disliked = recommend(session, ratings, limit=50).backlog
    assert liked.score > 0 > disliked.score
    assert disliked.similarity < 0


def test_review_quality_breaks_a_tie_between_identical_tags(session):
    ratings = build_scenario(session)
    good, bad = recommend(session, ratings, limit=50).discover
    assert good.similarity == pytest.approx(bad.similarity)
    assert good.quality > bad.quality
    assert good.score > bad.score


def test_limit_caps_each_list(session):
    ratings = build_scenario(session)
    assert len(recommend(session, ratings, limit=1).discover) == 1


def test_a_game_marked_not_interested_is_not_recommended(session):
    ratings = build_scenario(session)
    result = recommend(session, ratings, {40: "Pool Stealth Good"}, limit=50)
    assert [r.game.name for r in result.discover] == ["Pool Stealth Bad"]


def test_not_interested_pushes_that_games_tags_away(session):
    ratings = build_scenario(session)
    before = taste_profile(session, ratings, {}).profile["Stealth"]
    after = taste_profile(session, ratings, {40: "Pool Stealth Good"}).profile["Stealth"]
    assert after < before


def test_the_reason_names_the_games_you_played_and_the_matching_tags(session):
    ratings = build_scenario(session)
    top = recommend(session, ratings, limit=50).discover[0]
    assert any("Stealth Hero" in because for because in top.because)
    assert "Stealth" in top.tags


def test_a_game_is_never_compared_with_itself(session):
    ratings = build_scenario(session)
    why = explain(session, ratings, {}, 1)  # the rated game itself
    assert not any("Stealth Hero (" in because for because in why.because)


def test_explain_returns_nothing_for_a_game_without_tags(session):
    ratings = build_scenario(session)
    add_game(session, 99, "No Tags", owned=False)
    assert explain(session, ratings, {}, 99) is None


# ---- who counts as backlog or played ----


def test_under_an_hour_is_backlog_and_over_an_hour_is_played(session):
    ratings = build_scenario(session)
    add_game(session, 50, "Tried It", tags={"Stealth": 100}, minutes=59)
    add_game(session, 51, "Played It", tags={"Stealth": 100}, minutes=61)
    names = [r.game.name for r in recommend(session, ratings, limit=50).backlog]
    assert "Tried It" in names
    assert "Played It" not in names


def test_a_family_game_with_an_unlocked_achievement_counts_as_played(session):
    ratings = build_scenario(session)
    add_game(session, 52, "Played Family", tags={"Stealth": 100}, owned=False, family=True, achievements=(3, 20))
    names = [r.game.name for r in recommend(session, ratings, limit=50).family]
    assert "Played Family" not in names
    assert "Family Stealth" in names


def test_a_family_edition_of_a_game_you_own_is_not_suggested(session):
    ratings = build_scenario(session)
    add_game(session, 53, "Backlog Stealth (Legacy)", tags={"Stealth": 100}, owned=False, family=True)
    names = [r.game.name for r in recommend(session, ratings, limit=50).family]
    assert "Backlog Stealth (Legacy)" not in names


# ---- scoring everything at once ----


def test_score_all_scores_every_tagged_game_and_partitions_the_candidates(session):
    ratings = build_scenario(session)
    scores = score_all(session, ratings)
    assert scores.backlog == {20, 21}
    assert scores.family == {30}
    assert scores.discover == {40, 41}
    assert (scores.backlog | scores.family | scores.discover) <= set(scores.by_id)
    assert 1 in scores.by_id  # games you've played are scored too


def test_score_all_agrees_with_recommend(session):
    ratings = build_scenario(session)
    scores = score_all(session, ratings)
    for rec in recommend(session, ratings, limit=50).discover:
        assert scores.by_id[rec.game.steam_app_id].score == pytest.approx(rec.score)


# ---- what the diary status says about taste ----


def test_a_favorite_pulls_its_tags_in(session):
    build_scenario(session)
    ratings = {2: 1.0}  # game 1 (stealth) is unrated
    before = taste_profile(session, ratings, {}).profile.get("Stealth", 0.0)
    diary.set_favorite(session, 1, True)
    after = taste_profile(session, ratings, {}).profile["Stealth"]
    assert after > before and after > 0


def test_a_rating_beats_a_favorite(session):
    build_scenario(session)
    diary.save_rating(session, 1, 2.0)
    diary.set_favorite(session, 1, True)
    ratings = diary.load_ratings(session)
    assert taste_profile(session, ratings, {}).profile["Stealth"] < 0  # still the 2 stars


@pytest.mark.parametrize("status", ["playing", "played", "dropped"])
def test_a_status_alone_does_not_change_the_taste(session, status):
    # "Dropped" means too many things (a story game quit halfway, an endless
    # multiplayer game left after 600 hours) to say what you like.
    build_scenario(session)
    ratings = {1: 5.0, 2: 1.0}
    unrated = {2: 1.0}
    before = taste_profile(session, ratings, {}).profile
    before_unrated = taste_profile(session, unrated, {}).profile
    diary.set_status(session, 1, status)
    assert taste_profile(session, ratings, {}).profile == before
    assert taste_profile(session, unrated, {}).profile == before_unrated


@pytest.mark.parametrize("status", ["playing", "played", "dropped"])
def test_a_game_with_a_status_is_no_longer_a_candidate(session, status):
    build_scenario(session)
    add_game(session, 60, "Quit Quickly", tags={"Stealth": 100})  # owned, never played: backlog
    ratings = {1: 5.0, 2: 1.0}
    assert 60 in score_all(session, ratings).backlog
    diary.set_status(session, 60, status)
    assert 60 not in score_all(session, ratings).backlog


def test_a_game_you_do_not_own_with_a_status_is_not_recommended_to_discover(session):
    build_scenario(session)
    ratings = {1: 5.0, 2: 1.0}
    assert 40 in score_all(session, ratings).discover
    diary.set_status(session, 40, "played")  # played it elsewhere
    assert 40 not in score_all(session, ratings).discover


def test_a_favorite_is_not_described_as_rated_five(session):
    build_scenario(session)
    diary.set_favorite(session, 1, True)
    best = recommend(session, {2: 1.0}, limit=50).backlog[0]
    reasons = " ".join(best.because)
    assert "Stealth Hero" in reasons and "rated" not in reasons

