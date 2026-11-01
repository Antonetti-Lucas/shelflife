"""Bad page numbers in a link, a paged My ratings page, and messages that point to buttons, not commands."""

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.web.app import RATINGS_PAGE_SIZE, app, whole
from tests.factories import add_game, build_scenario


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("raw, value", [("3", 3), (" 7 ", 7), ("abc", 1), ("", 1), ("-2", 1), ("2.5", 1), ("²", 1), ("9" * 30, 1), (None, 1)])
def test_a_page_number_from_a_link_is_read_leniently(raw, value):
    assert whole(raw, 1) == value


@pytest.mark.parametrize(
    "path",
    ["/?bp=abc&size=lots", "/library?page=abc", "/explore?page=x", "/wishlist?page=", "/diary?page=-1", "/ratings?page=zz", "/library?page=99999999999"],
)
def test_a_bad_page_number_shows_the_page_instead_of_an_error(client, session, path):
    build_scenario(session)
    assert client.get(path).status_code == 200


def test_my_ratings_is_paged_and_keeps_your_place_after_an_edit(client, session):
    for i in range(1, RATINGS_PAGE_SIZE + 6):
        add_game(session, i, f"Game {i:03}", minutes=600)
        diary.save_rating(session, i, 4.0)
    first = client.get("/ratings").text
    assert f"{RATINGS_PAGE_SIZE + 5} games" in first and "Page 1 of 2" in first
    second = client.get("/ratings?page=2").text
    assert "Page 2 of 2" in second and second.count('class="row"') == 5
    assert 'name="next" value="/ratings?page=2#g' in second  # saving a rating returns to page 2


def test_empty_lists_point_to_buttons_not_commands(client, session):
    for path in ("/", "/stats"):
        assert "python -m" not in client.get(path).text
    assert 'href="/explore#find"' in client.get("/").text
