"""Lists you make and name: ranked or not, with your ratings shown or not, in your own order."""

import re

import pytest
from fastapi.testclient import TestClient

from shelflife import diary, gamelists
from shelflife.models import GameListEntry
from shelflife.recommend import score_all
from shelflife.web.app import app
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


@pytest.fixture
def games(session):
    for app_id, name in ((1, "Hades"), (2, "Celeste"), (3, "Portal 2"), (4, "Hollow Knight")):
        add_game(session, app_id, name)
    add_game(session, 5, "Outer Wilds", owned=False)
    add_game(session, 6, "Hades DLC", type="dlc")


def order(session, list_id):
    return [g.steam_app_id for _, g in gamelists.entries(session, list_id)]


def shown(html):
    """The games on a list's page, in the order shown."""
    return [int(x) for x in re.findall(r'data-app-id="(\d+)"', html)]


# ---- making and changing lists ----


def test_a_list_needs_a_name_and_keeps_it_tidy(session):
    with pytest.raises(ValueError):
        gamelists.create_list(session, "   ")
    made = gamelists.create_list(session, "  Best   of 2024 ", "  Mine.  ")
    assert (made.name, made.description, made.ranked, made.show_ratings) == ("Best of 2024", "Mine.", False, True)
    with pytest.raises(ValueError):
        gamelists.create_list(session, "x" * (gamelists.NAME_MAX + 1))


def test_games_go_on_the_end_once_and_leave_without_gaps(session, games):
    gl = gamelists.create_list(session, "Favourites")
    for app_id in (1, 2, 3):
        assert gamelists.add_game(session, gl.id, app_id)
    assert not gamelists.add_game(session, gl.id, 2)  # already on it
    gamelists.remove_game(session, gl.id, 1)
    assert order(session, gl.id) == [2, 3]
    assert [e.position for e, _ in gamelists.entries(session, gl.id)] == [1, 2]


def test_a_game_moves_to_any_place(session, games):
    gl = gamelists.create_list(session, "Ranked", ranked=True)
    for app_id in (1, 2, 3, 4):
        gamelists.add_game(session, gl.id, app_id)
    gamelists.move_game(session, gl.id, 4, 1)
    assert order(session, gl.id) == [4, 1, 2, 3]
    gamelists.move_game(session, gl.id, 4, 99)  # past the end: last
    assert order(session, gl.id) == [1, 2, 3, 4]


def test_a_dragged_order_must_name_every_game_once(session, games):
    gl = gamelists.create_list(session, "L")
    for app_id in (1, 2, 3):
        gamelists.add_game(session, gl.id, app_id)
    gamelists.set_order(session, gl.id, [3, 1, 2])
    assert order(session, gl.id) == [3, 1, 2]
    for wrong in ([3, 1], [3, 1, 2, 2], [3, 1, 4]):
        with pytest.raises(ValueError):
            gamelists.set_order(session, gl.id, wrong)
    assert order(session, gl.id) == [3, 1, 2]


def test_deleting_a_list_keeps_the_games_and_ratings(session, games):
    gl = gamelists.create_list(session, "Gone")
    gamelists.add_game(session, gl.id, 1)
    diary.save_rating(session, 1, 4.5)
    gamelists.delete_list(session, gl.id)
    assert gamelists.all_lists(session) == [] and session.query(GameListEntry).count() == 0
    assert diary.load_ratings(session) == {1: 4.5}


def test_the_search_finds_games_by_every_word_and_skips_ones_on_the_list(session, games):
    gl = gamelists.create_list(session, "L")
    gamelists.add_game(session, gl.id, 3)
    assert [g.steam_app_id for g in gamelists.addable(session, gl.id, "e")] == [2, 1, 5]  # owned first; the DLC is left out
    assert [g.steam_app_id for g in gamelists.addable(session, gl.id, "o")] == [4, 5]  # Portal 2 is on the list already
    assert [g.name for g in gamelists.addable(session, gl.id, "hollow kn")] == ["Hollow Knight"]
    assert gamelists.addable(session, gl.id, "  ") == []


def test_a_list_never_changes_your_recommendations(session):
    ratings = build_scenario(session)
    for app_id, rating in ratings.items():
        diary.save_rating(session, app_id, rating)
    def scores():
        found = score_all(session, ratings)
        return {app_id: r.score for app_id, r in found.by_id.items()}, (found.backlog, found.family, found.discover)

    before = scores()
    gl = gamelists.create_list(session, "Worst games ever", ranked=True)
    for app_id in list(before[0])[:3]:
        gamelists.add_game(session, gl.id, app_id)
    assert scores() == before


# ---- the pages ----


def test_making_a_list_from_the_lists_page(client, session, games):
    assert "No lists" not in client.get("/lists").text and "+ New list" in client.get("/lists").text
    response = post(client, "/lists", name="Cozy games", description="Rainy days", ranked="1", show_ratings="1")
    assert response.status_code == 303 and response.headers["location"].startswith("/lists/")
    page = client.get("/lists").text
    assert "Cozy games" in page and "0 games · ranked" in page and "Rainy days" in page


def test_a_list_without_a_name_is_refused(client):
    assert post(client, "/lists", name="").status_code == 400


def test_ranked_lists_show_numbers_and_unranked_ones_dont(client, session, games):
    gl = gamelists.create_list(session, "Top", ranked=True)
    for app_id in (2, 1):
        gamelists.add_game(session, gl.id, app_id)
    page = client.get(f"/lists/{gl.id}").text
    assert shown(page) == [2, 1] and '<span class="rank">1</span>' in page and '<span class="rank">2</span>' in page
    post(client, f"/lists/{gl.id}/option", option="ranked", value="0")
    page = client.get(f"/lists/{gl.id}").text
    assert 'class="rank"' not in page and shown(page) == [2, 1]  # unranked, still in your order


def test_your_stars_under_each_game_can_be_turned_off(client, session, games):
    gl = gamelists.create_list(session, "Stars")
    gamelists.add_game(session, gl.id, 1)
    diary.save_rating(session, 1, 4.0)
    page = client.get(f"/lists/{gl.id}").text
    assert 'class="starrate small"' in page and 'data-rating="4"' in page
    post(client, f"/lists/{gl.id}/option", option="show_ratings", value="0")
    assert 'class="starrate' not in client.get(f"/lists/{gl.id}").text
    assert post(client, f"/lists/{gl.id}/option", option="nonsense", value="1").status_code == 400


def test_adding_moving_dragging_and_removing_from_the_page(client, session, games):
    gl = gamelists.create_list(session, "L", ranked=True)
    page = client.get(f"/lists/{gl.id}?q=hades").text
    assert "Hades" in page and "+ Add" in page and "Hades DLC" not in page
    for app_id in (1, 2, 3):
        response = post(client, f"/lists/{gl.id}/add", app_id=str(app_id), next=f"/lists/{gl.id}?q=hades")
        assert response.headers["location"] == f"/lists/{gl.id}?q=hades"  # back to the search, to add more
    post(client, f"/lists/{gl.id}/move", app_id="3", to="1")
    assert shown(client.get(f"/lists/{gl.id}").text) == [3, 1, 2]
    assert post(client, f"/lists/{gl.id}/order", order="2,3,1").json() == {"saved": True}
    assert shown(client.get(f"/lists/{gl.id}").text) == [2, 3, 1]
    assert post(client, f"/lists/{gl.id}/order", order="2,3").status_code == 400
    post(client, f"/lists/{gl.id}/remove", app_id="3")
    assert shown(client.get(f"/lists/{gl.id}").text) == [2, 1]


def test_editing_and_deleting_a_list(client, session, games):
    gl = gamelists.create_list(session, "Old name")
    post(client, f"/lists/{gl.id}/edit", name="New name", description="", ranked="1")
    page = client.get(f"/lists/{gl.id}").text
    assert "New name" in page and 'aria-pressed="false">My ratings' in page  # the unticked box turned stars off
    assert post(client, f"/lists/{gl.id}/delete").headers["location"] == "/lists"
    assert client.get(f"/lists/{gl.id}").status_code == 404


def test_a_games_page_adds_it_to_a_list_or_starts_one(client, session, games):
    gl = gamelists.create_list(session, "Platformers")
    page = client.get("/game/2").text
    assert "Add to list" in page and "Platformers" in page
    post(client, "/game/2/lists", list_id=str(gl.id))
    page = client.get("/game/2").text
    assert f'On <a href="/lists/{gl.id}">Platformers</a>' in page and "Add to list" not in page  # no other list to offer
    response = post(client, "/lists", name="Best of 2018", app_id="2", show_ratings="1")
    new_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert order(session, new_id) == [2]


def test_lists_cannot_be_changed_from_another_website(client, session, games):
    gl = gamelists.create_list(session, "Mine")
    response = client.post(f"/lists/{gl.id}/delete", headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert response.status_code == 403 and gamelists.all_lists(session)


def test_the_nav_links_to_lists(client):
    assert '<a href="/lists" class="on">Lists</a>' in client.get("/lists").text
