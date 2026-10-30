"""Review quality and tag similarity."""

import math

import pytest

from shelflife.quality import QUALITY_EXPONENT, UNKNOWN_WILSON, quality_factor, wilson_lower_bound
from shelflife.tags import STOPLIST, TOP_N, compute_idf, cosine, tag_vector, tag_weights

# ---- review quality ----


def test_no_reviews_means_no_wilson_score():
    assert wilson_lower_bound(0, 0) is None


def test_few_reviews_are_trusted_less_than_many():
    assert wilson_lower_bound(6, 0) < wilson_lower_bound(38_000, 2_000)


def test_more_evidence_at_the_same_ratio_raises_the_bound():
    assert wilson_lower_bound(95, 5) < wilson_lower_bound(9_500, 500)


def test_a_perfect_score_from_six_reviews_is_far_from_perfect():
    assert wilson_lower_bound(6, 0) < 0.7


def test_unknown_reviews_get_a_slightly_below_average_factor():
    assert quality_factor(None, None) == pytest.approx(UNKNOWN_WILSON**QUALITY_EXPONENT)
    assert quality_factor(0, 0) == pytest.approx(UNKNOWN_WILSON**QUALITY_EXPONENT)


def test_well_reviewed_games_get_about_point_nine():
    assert 0.88 < quality_factor(38_000, 2_000) < 0.91


def test_mixed_reviews_get_about_a_half():
    assert 0.45 < quality_factor(7_000, 3_000) < 0.52


def test_quality_orders_games_by_reviews():
    assert quality_factor(9_000, 500) > quality_factor(300, 300)


# ---- tags ----


def test_tags_that_are_not_about_gameplay_are_ignored():
    assert "Early Access" in STOPLIST
    assert tag_weights({"Early Access": 1000, "Stealth": 100}) == {"Stealth": 1.0}


def test_tag_weight_is_the_vote_share_of_the_top_tag():
    assert tag_weights({"A": 200, "B": 100}) == {"A": 1.0, "B": 0.5}


def test_only_the_top_tags_count():
    tags = {f"Tag{i}": 1000 - i for i in range(30)}
    weights = tag_weights(tags)
    assert len(weights) == TOP_N
    assert "Tag0" in weights and "Tag29" not in weights


def test_no_tags_or_no_votes_give_nothing():
    assert tag_weights({}) == {}
    assert tag_weights({"A": 0}) == {}


def test_a_tag_on_every_game_has_no_weight_and_a_rare_tag_has_some():
    games = [{"Indie": 10, "Rare": 10}, {"Indie": 10}, {"Indie": 10}]
    idf = compute_idf(games)
    assert idf["Indie"] == pytest.approx(0)
    assert idf["Rare"] == pytest.approx(math.log(4 / 2))


def test_games_without_tags_do_not_change_rarity():
    games = [{"Indie": 10, "Rare": 10}, {"Indie": 10}, {"Indie": 10}]
    assert compute_idf(games + [{}, {}]) == compute_idf(games)


def test_unknown_tags_get_zero_weight_in_a_vector():
    assert tag_vector({"Mystery": 10}, {}) == {"Mystery": 0.0}


def test_vector_scales_vote_share_by_rarity():
    assert tag_vector({"A": 100, "B": 50}, {"A": 2.0, "B": 4.0}) == {"A": 2.0, "B": 2.0}


def test_identical_vectors_have_similarity_one():
    assert cosine({"A": 1, "B": 2}, {"A": 1, "B": 2}) == pytest.approx(1)


def test_vectors_with_no_shared_tags_have_similarity_zero():
    assert cosine({"A": 1}, {"B": 1}) == 0


def test_a_disliked_tag_gives_negative_similarity():
    assert cosine({"Sports": -1.0}, {"Sports": 1.0}) == pytest.approx(-1)


def test_empty_vectors_do_not_divide_by_zero():
    assert cosine({}, {"A": 1}) == 0
    assert cosine({"A": 1}, {}) == 0
