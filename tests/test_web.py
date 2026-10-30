"""The web app, driven the way a browser would."""

import re

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.db import SessionLocal
from shelflife.models import LibraryEntry
from shelflife.recommend import score_all
from shelflife.web.app import app
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def scenario(session):
    ratings = build_scenario(session)
    for app_id, rating in ratings.items():
        diary.save_rating(session, app_id, rating)
    return ratings


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def diary_entry(app_id):
    with SessionLocal() as s:
        return diary.get_entry(s, app_id)


def minutes(app_id):
    with SessionLocal() as s:
        return s.get(LibraryEntry, app_id).playtime_total_min


def names_on(html):
    return re.findall(r'<a href="/game/\d+"[^>]*>([^<]+)</a>', html)


# ---- pages ----


@pytest.mark.parametrize("path", ["/", "/library", "/explore", "/ratings", "/taste"])
def test_every_page_loads_with_an_empty_database(client, path):
    assert client.get(path).status_code == 200


def test_the_nav_links_every_page(client):
    html = client.get("/").text
    for target in ("/", "/library", "/explore", "/ratings", "/taste"):
        assert f'href="{target}"' in html


def test_the_home_page_shows_all_three_lists(client, scenario):
    html = client.get("/").text
    for name in ("Backlog Stealth", "Family Stealth", "Pool Stealth Good"):
        assert name in html


def test_the_recommendation_lists_are_paged(client, scenario, session):
    for i in range(100, 120):
        add_game(session, i, f"Extra {i}", tags={"Stealth": 100}, owned=False, reviews=(9000, 500))
    first = client.get("/?size=6").text
    # 6 of the 22 discover games, plus the 2 backlog and 1 family candidates
    assert len(re.findall(r'class="card"', first)) == 6 + 2 + 1
    page_two = client.get("/?size=6&dp=2").text
    assert 'href="/?size=6&amp;bp=1&amp;fp=1&amp;dp=1#discover"' in page_two  # "Previous" keeps the other lists' pages
    assert client.get("/?size=7&bp=999&fp=-3&dp=2").status_code == 200  # odd values are tolerated


def test_a_card_remembers_which_page_to_return_to(client, scenario, session):
    for i in range(100, 120):
        add_game(session, i, f"Extra {i}", tags={"Stealth": 100}, owned=False, reviews=(9000, 500))
    html = client.get("/?size=6&dp=2").text
    assert "dp=2#discover" in html


# ---- library ----


def test_the_library_shows_only_games_you_own_or_can_play_through_family(client, scenario):
    html = client.get("/library").text
    assert "Backlog Stealth" in html and "Family Stealth" in html
    assert "Pool Stealth Good" not in html


def test_only_family_rows_have_an_hours_box(client, scenario):
    assert client.get("/library?source=owned").text.count('name="hours"') == 0
    assert client.get("/library?source=family").text.count('name="hours"') == 1


def test_every_row_has_a_rating_dropdown_with_the_current_rating_selected(client, scenario):
    html = client.get("/library?q=Stealth+Hero").text
    assert 'name="rating"' in html
    assert re.search(r'<option value="5.0" selected', html)


def test_the_recommended_filter_keeps_the_best_fit_candidates_only(client, scenario, session):
    scores = score_all(session, scenario)
    scope = scores.backlog | scores.family
    best = max(scope, key=lambda i: scores.by_id[i].score)
    html = client.get("/library?rec=10").text
    assert f'id="g{best}"' in html
    assert len(re.findall(r'<tr id="g\d+"', html)) == 1
    assert len(re.findall(r'<tr id="g\d+"', client.get("/library").text)) > 1


def test_recommended_sorts_by_fit_automatically(client, scenario):
    assert 'value="fit" selected' in client.get("/library?rec=50").text


def test_library_search_is_case_insensitive_and_escaped(client, scenario):
    assert "Backlog Stealth" in client.get("/library?q=backlog+stealth").text
    assert "<script>alert(1)</script>" not in client.get("/library?q=<script>alert(1)</script>").text


# ---- explore ----


def test_explore_shows_only_games_you_do_not_have(client, scenario):
    html = client.get("/explore").text
    assert "Pool Stealth Good" in html
    assert "Backlog Stealth" not in html and "Family Stealth" not in html


def test_explore_hides_dismissed_games(client, scenario):
    post(client, "/game/40/not-interested", value="1")
    assert "Pool Stealth Good" not in client.get("/explore").text
    post(client, "/game/40/not-interested", value="0")
    assert "Pool Stealth Good" in client.get("/explore").text


def test_a_rated_game_stays_in_explore_with_its_rating_shown(client, scenario):
    post(client, "/game/40/rate", rating="3.5")
    html = client.get("/explore").text
    assert "Pool Stealth Good" in html
    assert re.search(r'<option value="3.5" selected', html)


def test_explore_orders_by_fit_and_can_be_searched_and_filtered(client, scenario):
    html = client.get("/explore").text
    assert html.index("Pool Stealth Good") < html.index("Pool Stealth Bad")
    assert "Pool Stealth Bad" not in client.get("/explore?q=good").text
    assert len(re.findall(r'class="card"', client.get("/explore?rec=10").text)) == 1


# ---- ratings and reviews ----


def test_rating_and_reviewing_a_game(client, scenario):
    response = post(client, "/game/20/rate", rating="4", review="nice")
    assert response.status_code == 303
    entry = diary_entry(20)
    assert (entry.rating, entry.review) == (4.0, "nice")


def test_clearing_only_the_rating_keeps_the_review(client, scenario):
    post(client, "/game/20/rate", rating="4", review="nice")
    post(client, "/game/20/rate", rating="")  # no review field: leave it alone
    entry = diary_entry(20)
    assert (entry.rating, entry.review) == (None, "nice")


def test_clearing_both_rating_and_review_removes_the_entry(client, scenario):
    post(client, "/game/20/rate", rating="4", review="nice")
    post(client, "/game/20/rate", rating="", review="")  # the "Delete" button
    assert diary_entry(20) is None


def test_a_blank_rating_on_an_unrated_game_does_not_crash(client, scenario):
    assert post(client, "/game/20/rate", rating="").status_code == 303


@pytest.mark.parametrize("rating", ["7", "0", "4.3", "abc"])
def test_bad_ratings_are_rejected(client, scenario, rating):
    assert post(client, "/game/20/rate", rating=rating).status_code == 400
    assert diary_entry(20) is None


def test_rating_a_game_that_does_not_exist_is_an_error(client, scenario):
    assert post(client, "/game/999999/rate", rating="4").status_code == 400


def test_the_ratings_page_lists_ratings_reviews_and_dismissed_games(client, scenario):
    post(client, "/game/20/rate", rating="4", review="a <b>bold</b> review")
    post(client, "/game/21/not-interested", value="1")
    html = client.get("/ratings").text
    assert "Backlog Stealth" in html and "Backlog Sports" in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>bold</b>" not in html  # reviews are escaped
    assert "Delete rating and review" in html and "Undo" in html


def test_not_interested_teaches_the_taste_and_can_be_undone(client, scenario):
    before = client.get("/taste").text
    post(client, "/game/40/not-interested", value="1")
    assert "Pool Stealth Good" not in client.get("/").text
    post(client, "/game/40/not-interested", value="0")
    assert client.get("/taste").text == before


# ---- hours ----


def test_hours_can_be_entered_for_a_family_game(client, scenario):
    assert post(client, "/game/30/hours", hours="12,5").status_code == 303
    assert minutes(30) == 750
    assert 'value="12.5"' in client.get("/game/30").text


def test_hours_can_be_cleared(client, scenario):
    post(client, "/game/30/hours", hours="12")
    post(client, "/game/30/hours", hours="")
    assert minutes(30) == 0


@pytest.mark.parametrize("hours", ["abc", "-1", "999999"])
def test_bad_hours_are_rejected(client, scenario, hours):
    assert post(client, "/game/30/hours", hours=hours).status_code == 400


def test_hours_are_refused_for_a_game_you_own(client, scenario):
    assert post(client, "/game/20/hours", hours="5").status_code == 400


# ---- game page ----


def test_the_game_page_explains_the_score_and_recency(client, scenario):
    html = client.get("/game/1").text
    assert "Engagement score" in html and "recency" in html
    assert "Your rating decides" in html  # it is rated, so the engagement score is not used


def test_a_missing_game_is_a_404(client):
    assert client.get("/game/424242").status_code == 404


# ---- security ----


def test_a_form_posted_from_another_website_is_blocked(client, scenario):
    response = client.post(
        "/game/20/rate", data={"rating": "5"}, headers={"Origin": "https://evil.example"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert diary_entry(20) is None


def test_a_post_with_no_origin_header_is_allowed(client, scenario):
    # Command-line tools and some browsers send no Origin; the app is local-only.
    assert client.post("/game/20/rate", data={"rating": "5"}, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "javascript:alert(1)", ""])
def test_redirects_never_leave_the_site(client, scenario, target):
    response = post(client, "/game/20/rate", rating="", next=target)
    assert response.headers["location"] == "/game/20"


def test_redirects_to_a_page_on_the_site_are_kept_with_their_anchor(client, scenario):
    response = post(client, "/game/20/rate", rating="", next="/library?source=family&rec=25#g20")
    assert response.headers["location"] == "/library?source=family&rec=25#g20"
