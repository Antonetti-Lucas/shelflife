"""Hours for family games, the family import, and linking ranked games to app ids."""

import json
from pathlib import Path

import pytest

from shelflife import diary
from shelflife.family import FamilyApp, import_family, load_export, shared_with_you
from shelflife.hours import apply_hours, load_hours, set_hours, write_template
from shelflife.models import Game, LibraryEntry
from shelflife.rankings import RankedGame, link_rankings, load_rankings, save_rankings, suggest_links
from tests.factories import add_game

ME = "steamid-of-me"  # obviously fake: real SteamIDs never belong in the repository
OTHER = "steamid-of-someone-else"

# ---- hours ----


def test_hours_can_be_set_for_a_family_game(session):
    add_game(session, 1, owned=False, family=True)
    set_hours(session, 1, 12.5)
    assert session.get(LibraryEntry, 1).playtime_total_min == 750


def test_hours_can_be_cleared(session):
    add_game(session, 1, owned=False, family=True, minutes=600)
    set_hours(session, 1, None)
    assert session.get(LibraryEntry, 1).playtime_total_min == 0


@pytest.mark.parametrize("hours", [-1, 100_001])
def test_hours_out_of_range_are_rejected(session, hours):
    add_game(session, 1, owned=False, family=True)
    with pytest.raises(ValueError):
        set_hours(session, 1, hours)


def test_hours_are_refused_for_games_you_own(session):
    add_game(session, 1)  # Steam reports hours for owned games; a sync would overwrite ours
    with pytest.raises(ValueError):
        set_hours(session, 1, 5)


def test_hours_are_refused_for_games_you_do_not_have(session):
    add_game(session, 1, owned=False)
    with pytest.raises(ValueError):
        set_hours(session, 1, 5)


def write_hours_csv(text):
    Path("data").mkdir(exist_ok=True)
    Path("data/hours.csv").write_text(text, encoding="utf-8-sig")


def test_hours_file_accepts_decimal_commas_and_skips_blank_rows():
    write_hours_csv('steam_app_id,name,hours\n1,A,"24,5"\n2,B,\n3,C,10\n')
    assert load_hours() == {1: 24.5, 3: 10.0}


def test_hours_file_accepts_semicolon_separators_like_excel_in_some_locales():
    write_hours_csv("steam_app_id;name;hours\n1;A;24,5\n2;B;\n")
    assert load_hours() == {1: 24.5}


def test_hours_file_rejects_negative_hours():
    write_hours_csv("steam_app_id,name,hours\n1,A,-3\n")
    with pytest.raises(ValueError):
        load_hours()


def test_a_missing_hours_file_means_no_hours():
    assert load_hours() == {}


def test_applying_hours_sorts_games_into_applied_skipped_and_unknown(session):
    add_game(session, 1, owned=False, family=True)
    add_game(session, 2)  # owned
    result = apply_hours(session, {1: 10.0, 2: 5.0, 3: 1.0})
    assert [g.steam_app_id for g, _ in result.applied] == [1]
    assert [g.steam_app_id for g in result.owned_skipped] == [2]
    assert result.unknown == [3]
    assert session.get(LibraryEntry, 1).playtime_total_min == 600


def test_template_lists_family_games_you_probably_played_and_keeps_entered_hours(session):
    add_game(session, 1, "Played", owned=False, family=True, achievements=(5, 20))
    add_game(session, 2, "Ranked", owned=False, family=True)
    add_game(session, 3, "Untouched", owned=False, family=True)
    add_game(session, 4, "Owned", achievements=(5, 20))
    save_rankings([RankedGame(2026, 1, "Ranked", 2)])

    assert write_template(session) == 2  # Played (achievement) and Ranked (ranking)
    text = Path("data/hours.csv").read_text(encoding="utf-8-sig")
    assert "Untouched" not in text and "Owned" not in text

    text = text.replace("Played,,", "Played,30,")  # the user types hours for one game
    Path("data/hours.csv").write_text(text, encoding="utf-8-sig")
    assert write_template(session) == 0  # nothing new to add
    assert load_hours() == {1: 30.0}  # and what was typed is kept


# ---- family import ----


def app(app_id, owners=(OTHER,), reason=0, name=None):
    return FamilyApp(app_id, tuple(owners), reason, name)


def test_only_games_shared_with_you_and_playable_are_imported():
    apps = [app(1), app(2, owners=(ME, OTHER)), app(3, reason=3), app(4, owners=(ME,))]
    assert [a.app_id for a in shared_with_you(apps, ME)] == [1]


def test_family_games_are_added_with_a_library_entry_and_no_hours(session):
    result = import_family(session, [app(1, name="Shared Game")], {})
    game = session.get(Game, 1)
    assert result.added == 1
    assert (game.name, game.family_shared) == ("Shared Game", True)
    assert session.get(LibraryEntry, 1).playtime_total_min == 0


def test_names_come_from_the_export_then_the_lookup_then_a_placeholder(session):
    import_family(session, [app(1, name="From Export"), app(2), app(3)], {2: "From Lookup"})
    assert [session.get(Game, i).name for i in (1, 2, 3)] == ["From Export", "From Lookup", "App 3"]


def test_importing_twice_adds_nothing_new(session):
    import_family(session, [app(1)], {})
    again = import_family(session, [app(1)], {})
    assert (again.added, again.already_known) == (0, 1)


def test_a_game_you_own_is_left_alone(session):
    add_game(session, 1, "Mine", minutes=600)
    import_family(session, [app(1)], {})
    assert session.get(Game, 1).family_shared is False
    assert session.get(LibraryEntry, 1).playtime_total_min == 600


def test_a_pool_game_that_becomes_shared_turns_into_a_family_game(session):
    add_game(session, 1, "Was In The Pool", owned=False)
    import_family(session, [app(1)], {})
    assert session.get(Game, 1).family_shared is True
    assert session.get(LibraryEntry, 1) is not None


def test_games_no_longer_shared_are_removed_from_the_library(session):
    import_family(session, [app(1), app(2)], {})
    result = import_family(session, [app(1)], {})
    assert result.pruned == 1
    assert session.get(LibraryEntry, 2) is None
    assert session.get(Game, 2).family_shared is False


def test_a_valid_export_file_loads(tmp_path):
    path = tmp_path / "family.json"
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "owner_steamid": ME,
                "apps": [{"appid": 5, "owner_steamids": [OTHER], "exclude_reason": 0, "name": "X"}],
            }
        )
    )
    owner, apps = load_export(path)
    assert owner == ME
    assert apps == [FamilyApp(5, (OTHER,), 0, "X")]


@pytest.mark.parametrize("content", [{"format": 2, "apps": []}, {"format": 1}, {"apps": []}])
def test_a_file_that_is_not_a_family_export_is_rejected(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        load_export(path)


# ---- rankings ----


def test_rankings_survive_a_save_and_load_including_unlinked_games():
    save_rankings([RankedGame(2025, 1, "Linked", 10), RankedGame(2025, 2, "Unlinked", None)])
    loaded = load_rankings()
    assert [(r.position, r.name, r.app_id) for r in loaded] == [(1, "Linked", 10), (2, "Unlinked", None)]


def test_ranked_games_link_by_exact_title_ignoring_case_and_symbols(session):
    add_game(session, 10, "HITMAN™ 2")
    rows = [RankedGame(2025, 1, "Hitman 2", None)]
    assert [r.name for r in link_rankings(session, rows)] == ["Hitman 2"]
    assert rows[0].app_id == 10


def test_a_ranked_title_links_to_the_one_longer_title_that_starts_with_it(session):
    add_game(session, 10, "Shantae: Half-Genie Hero Ultimate Edition")
    rows = [RankedGame(2025, 1, "Shantae: Half-Genie Hero", None)]
    link_rankings(session, rows)
    assert rows[0].app_id == 10


def test_an_ambiguous_title_is_not_linked_but_the_options_are_suggested(session):
    for app_id, name in [(1, "Peggle Deluxe"), (2, "Peggle Extreme"), (3, "Peggle Nights")]:
        add_game(session, app_id, name)
    rows = [RankedGame(2025, 1, "Peggle", None)]
    assert link_rankings(session, rows) == []
    assert rows[0].app_id is None
    assert {g.name for g in suggest_links(session, rows)[0]} == {"Peggle Deluxe", "Peggle Extreme", "Peggle Nights"}


def test_an_exact_match_wins_over_longer_titles(session):
    add_game(session, 1, "Hades")
    add_game(session, 2, "Hades II")
    rows = [RankedGame(2025, 1, "Hades", None)]
    link_rankings(session, rows)
    assert rows[0].app_id == 1


def test_already_linked_rankings_are_not_changed(session):
    add_game(session, 1, "Hades")
    rows = [RankedGame(2025, 1, "Hades", 999)]
    assert link_rankings(session, rows) == []
    assert rows[0].app_id == 999
