"""The app answers only to this computer's own names, so a web page can't reach it through DNS rebinding."""

import pytest
from fastapi.testclient import TestClient

from shelflife.web.app import ALLOWED_HOSTS, app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_the_usual_names_are_allowed():
    assert {"127.0.0.1", "localhost"} <= set(ALLOWED_HOSTS)


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "127.0.0.1"])
def test_pages_open_under_this_computers_own_names(client, host):
    assert client.get("/stats", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example"])
def test_a_page_reached_under_another_name_is_refused(client, host):
    assert client.get("/stats", headers={"Host": host}).status_code == 400


def test_a_rebound_page_cannot_change_your_diary_even_from_its_own_origin(client):
    # After DNS rebinding the attacker's page is "same site" to the browser: Origin and Host match.
    headers = {"Host": "evil.example:8000", "Origin": "http://evil.example:8000"}
    response = client.post("/taste/mode", data={"mode": "ratings"}, headers=headers, follow_redirects=False)
    assert response.status_code == 400
