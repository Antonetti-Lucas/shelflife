"""Filtering games by community tag."""

import pytest

from shelflife.tagfilter import TOP_TAGS, TagIndex, has_all, parse_tags, top_tag_names


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Roguelike", ["Roguelike"]),
        (" Roguelike ,  Co-op ", ["Roguelike", "Co-op"]),
        ("a,,b,", ["a", "b"]),
        ("Co-op, co-op, CO-OP", ["Co-op"]),  # no repeats, ignoring case
        ("", []),
        (None, []),
        (" , ", []),
    ],
)
def test_the_tag_box_is_split_on_commas(raw, expected):
    assert parse_tags(raw) == expected


def test_only_the_most_voted_tags_count_as_the_games_tags():
    tags = {f"Tag {i}": 100 - i for i in range(TOP_TAGS + 5)}
    names = top_tag_names(tags)
    assert len(names) == TOP_TAGS
    assert "tag 0" in names and f"tag {TOP_TAGS}" not in names


def test_matching_ignores_case_and_needs_the_whole_name():
    tags = {"Online Co-Op": 50, "Roguelike": 40}
    assert has_all(tags, ["roguelike"]) and has_all(tags, ["ONLINE CO-OP"])
    assert not has_all(tags, ["co-op"])  # not a part of "Online Co-Op"
    assert not has_all(tags, ["rogue"])


def test_a_game_needs_every_wanted_tag():
    tags = {"A": 3, "B": 2}
    assert has_all(tags, ["a", "b"]) and not has_all(tags, ["a", "c"])


def test_no_wanted_tags_matches_everything_even_a_game_without_tags():
    assert has_all({}, []) and has_all(None, [])
    assert not has_all(None, ["A"])


def test_the_index_lists_tags_by_how_many_games_have_them():
    index = TagIndex([{"Common": 5, "Rare": 1}, {"Common": 9}, {"common": 2, "Other": 4}, None])
    assert index.popular()[0] == "Common"
    assert index.popular() == ["Common", "Other", "Rare"]  # ties alphabetical
    assert index.popular(limit=1) == ["Common"]


def test_the_index_tells_unknown_tags_from_known_ones():
    index = TagIndex([{"Stealth": 5}])
    assert index.unknown(["stealth", "Racing", "FPS"]) == ["Racing", "FPS"]
    assert index.unknown([]) == []
