"""Combining several tags: all of them, or any of them."""

import pytest

from shelflife.tagfilter import has_any, matches, parse_match

TAGS = {"Stealth": 100, "Comedy": 80, "Action": 60}


@pytest.mark.parametrize("raw, expected", [("any", "any"), (" ANY ", "any"), ("all", "all"), ("", "all"), (None, "all"), ("nonsense", "all")])
def test_the_match_choice_defaults_to_all(raw, expected):
    assert parse_match(raw) == expected


def test_any_needs_just_one_of_the_tags():
    assert has_any(TAGS, ["stealth", "Racing"])
    assert not has_any(TAGS, ["Racing", "Cooking"])


def test_no_wanted_tags_matches_everything_in_both_modes():
    assert has_any({}, []) and has_any(None, [])
    assert matches(None, [], "any") and matches(None, [], "all")


def test_all_and_any_differ_exactly_when_only_some_tags_are_present():
    wanted = ["Stealth", "Racing"]
    assert matches(TAGS, wanted, "any") is True
    assert matches(TAGS, wanted, "all") is False
    assert matches(TAGS, ["Stealth", "Comedy"], "all") is True


def test_any_still_ignores_case_and_needs_the_whole_name():
    assert matches({"Online Co-Op": 5}, ["online co-op"], "any")
    assert not matches({"Online Co-Op": 5}, ["co-op"], "any")


def test_any_ignores_tags_outside_the_games_top_twenty():
    tags = {f"Tag {i}": 1000 - i for i in range(25)}
    assert not has_any(tags, ["Tag 24"])
    assert has_any(tags, ["Tag 24", "Tag 0"])
