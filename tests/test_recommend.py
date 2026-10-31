"""The recommender: taste weights, candidate lists, and why a game is recommended."""

import pytest

from shelflife import diary
from shelflife.recommend import _base_name, explain, recommend, score_all, taste_profile, taste_weight
from tests.factories import add_game, build_scenario

# ---- taste weights ----


@pytest.mark.parametrize("rating, weight", [(5.0, 1.0), (4.0, 0.5), (3.0, 0.0), (2.0, -0.5), (1.0, -1.0)])
def test_a_rating_maps_to_a_weight_between_minus_one_and_one(rating, weight):
    assert taste_weight(None, rating) == pytest.approx(weight)


def test_an_unrated_game_with_no_library_entry_has_no_weight():
    assert taste_weight(None, None) == 0


def test_a_very_engaging_unrated_game_counts_for_at_most_half_a_five_star_rating(session):
    entry = add_game(session, 1, minutes=1000 * 60, achievements=(10, 10), rarest=2.0).library_entry
    assert taste_weight(entry, None) == pytest.approx(0.5)


def test_a_barely_engaging_unrated_game_has_no_weight(session):
    assert taste_weight(add_game(session, 1, minutes=600).library_entry, None) == 0


def test_in_the_mix_play_lifts_a_rating_but_the_rating_leads(session):
    entry = add_game(session, 1, minutes=1000 * 60, achievements=(10, 10), rarest=2.0).library_entry
    # a 2-star game (-0.5) played a lot (engagement fraction 1): 0.7 * -0.5 + 0.3 * 1
    assert taste_weight(entry, 2.0) == pytest.approx(-0.05)
    assert taste_weight(entry, 2.0, "mix") == pytest.approx(-0.05)
    assert taste_weight(entry, 2.0) < 0  # a low rating still pulls tags away


def test_in_the_mix_a_barely_played_game_counts_by_its_rating_alone(session):
    entry = add_game(session, 1, minutes=600).library_entry
    assert taste_weight(entry, 5.0) == pytest.approx(0.7)  # 70% of a 5-star game
    assert taste_weight(entry, 1.0) == pytest.approx(-0.7)


def test_a_game_played_elsewhere_counts_by_its_rating_alone_in_the_mix():
    assert taste_weight(None, 4.0) == pytest.approx(0.5)


# ---- the three ways of learning your taste ----


@pytest.fixture
def played_a_lot(session):
    return add_game(session, 1, minutes=1000 * 60, achievements=(10, 10), rarest=2.0).library_entry


@pytest.fixture
def barely_played(session):
    return add_game(session, 2, minutes=600).library_entry


def test_ratings_only_ignores_how_you_played(session, played_a_lot, barely_played):
    assert taste_weight(played_a_lot, 2.0, "ratings") == pytest.approx(-0.5)  # the rating decides
    assert taste_weight(barely_played, 5.0, "ratings") == pytest.approx(1.0)
    assert taste_weight(played_a_lot, None, "ratings") == 0  # a hundred hours teach nothing
    assert taste_weight(None, 4.0, "ratings") == pytest.approx(0.5)


def test_engagement_only_ignores_ratings(session, played_a_lot, barely_played):
    assert taste_weight(played_a_lot, 1.0, "engagement") == pytest.approx(1.0)  # a hated game, played a lot
    assert taste_weight(played_a_lot, None, "engagement") == pytest.approx(1.0)
    assert taste_weight(barely_played, 5.0, "engagement") == 0  # a loved game, barely played
    assert taste_weight(None, 5.0, "engagement") == 0  # played elsewhere: no play data


@pytest.mark.parametrize("mode", ["mix", "ratings", "engagement"])
def test_no_signal_means_no_weight_in_any_mode(mode):
    assert taste_weight(None, None, mode) == 0


def test_the_default_is_the_mix(session, played_a_lot):
    assert taste_weight(played_a_lot, 2.0) == taste_weight(played_a_lot, 2.0, "mix")


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

