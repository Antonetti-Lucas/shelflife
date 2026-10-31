"""SteamSpy's quirky tag lists: letter case, old names, and the stand-in list for unknown tags."""

import pytest

from shelflife import store
from shelflife.store import SpyListing, UNKNOWN_TAG_LIST_SIZE, fetch_spy_tag_games, spy_tag_spellings


@pytest.fixture(autouse=True)
def fresh_memory(monkeypatch):
    monkeypatch.setattr(store, "_spy_spellings", {})


def games(n, start=1):
    return [SpyListing(start + i, f"Game {start + i}", 900, 100) for i in range(n)]


@pytest.fixture
def spy(monkeypatch):
    """A SteamSpy that knows some tags only under certain spellings."""
    known, asked = {}, []

    def fetch(tag, timeout=90):
        asked.append(tag)
        if tag in known:
            return known[tag]
        return games(UNKNOWN_TAG_LIST_SIZE, start=9000) if tag.islower() or "-" in tag else []

    monkeypatch.setattr(store, "fetch_spy_tag_list", fetch)
    fetch.known, fetch.asked = known, asked
    return fetch


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("Metroidvania", ["Metroidvania", "metroidvania"]),
        ("Souls-like", ["Souls-like", "souls-like", "Souls-Like"]),
        ("Roguelike", ["Roguelike", "roguelike", "rogue-like"]),
        ("co-op", ["co-op", "Co-Op"]),
        ("RPG", ["RPG", "rpg", "Rpg"]),
    ],
)
def test_the_spellings_to_try(tag, expected):
    assert spy_tag_spellings(tag) == expected


def test_a_tag_that_works_as_given_is_asked_once(spy):
    spy.known["Stealth"] = games(30)
    assert len(fetch_spy_tag_games("Stealth")) == 30 and spy.asked == ["Stealth"]


def test_the_letter_case_steamspy_wants_is_found(spy):
    spy.known["metroidvania"] = games(50)
    assert len(fetch_spy_tag_games("Metroidvania")) == 50
    assert spy.asked == ["Metroidvania", "metroidvania"]


def test_hyphenated_tags_find_their_capitalised_form(spy):
    spy.known["Souls-Like"] = games(40)
    assert len(fetch_spy_tag_games("Souls-like")) == 40


def test_steams_new_name_finds_the_old_one(spy):
    spy.known["rogue-like"] = games(60)
    assert len(fetch_spy_tag_games("Roguelike")) == 60


def test_the_stand_in_list_for_unknown_tags_is_not_taken_for_games(spy):
    assert fetch_spy_tag_games("Nonsense") == []  # every spelling gets the 70-game stand-in or nothing


def test_a_list_that_is_not_the_stand_in_wins_over_it(spy):
    spy.known["Tag"] = games(UNKNOWN_TAG_LIST_SIZE + 1)
    assert len(fetch_spy_tag_games("Tag")) == UNKNOWN_TAG_LIST_SIZE + 1


def test_the_spelling_that_worked_is_remembered_so_it_is_not_searched_for_again(spy):
    spy.known["rpg"] = games(25)
    fetch_spy_tag_games("RPG")
    spy.asked.clear()
    fetch_spy_tag_games("RPG")
    assert spy.asked == ["rpg"]


def test_a_tag_nobody_lists_asks_each_spelling_once_and_returns_nothing(spy):
    assert fetch_spy_tag_games("Typo") == []
    assert spy.asked == spy_tag_spellings("Typo")


@pytest.mark.parametrize("tag, steamspy_name", [("FPS", "fps"), ("Shooter", "shooter"), ("First-Person", "first-person"), ("Indie", "indie")])
def test_tags_steamspy_only_lists_in_lowercase_are_found_from_the_capitalised_name(spy, tag, steamspy_name):
    # Regression: searching for "FPS" found nothing because SteamSpy answers only to "fps".
    spy.known[steamspy_name] = games(200)
    assert len(fetch_spy_tag_games(tag)) == 200
