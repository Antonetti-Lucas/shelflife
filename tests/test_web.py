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


def titles(html):
    """The game cards' titles, in the order shown."""
    return re.findall(r'<h3><a href="/game/\d+">([^<]+)</a></h3>', html)


def diary_entry(app_id):
    with SessionLocal() as s:
        return diary.get_entry(s, app_id)


def minutes(app_id):
    with SessionLocal() as s:
        return s.get(LibraryEntry, app_id).playtime_total_min


def names_on(html):
    return re.findall(r'<a href="/game/\d+"[^>]*>([^<]+)</a>', html)


# ---- pages ----


@pytest.mark.parametrize("path", ["/", "/library", "/explore", "/wishlist", "/diary", "/ratings", "/stats", "/taste", "/sync"])
def test_every_page_loads_with_an_empty_database(client, path):
    assert client.get(path).status_code == 200


def test_the_nav_links_every_page(client):
    html = client.get("/").text
    for target in ("/", "/library", "/explore", "/wishlist", "/diary", "/ratings", "/stats", "/taste", "/sync"):
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
    assert 'href="/?size=6&amp;bp=1&amp;fp=1&amp;wp=1&amp;dp=1#discover"' in page_two  # "Previous" keeps the other lists' pages
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


def test_every_row_has_stars_showing_the_current_rating(client, scenario):
    html = client.get("/library?q=Stealth+Hero").text
    assert 'class="starrate small"' in html and 'data-rating="5"' in html
    assert re.search(r'name="rating" value="5"[^>]*aria-pressed="true"', html)


def test_the_recommended_filter_keeps_the_best_fit_candidates_only(client, scenario, session):
    scores = score_all(session, scenario)
    scope = scores.backlog | scores.family
    best = max(scope, key=lambda i: scores.by_id[i].score)
    html = client.get("/library?rec=10").text
    assert f'id="g{best}"' in html
    assert len(re.findall(r'<tr id="g\d+"', html)) == 1
    assert len(re.findall(r'<tr id="g\d+"', client.get("/library").text)) > 1


def test_recommended_sorts_by_fit_automatically(client, scenario):
    assert 'name="sort" value="fit"' in client.get("/library?rec=50").text


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
    assert "Pool Stealth Good" not in titles(client.get("/explore").text)
    post(client, "/game/40/not-interested", value="0")
    assert "Pool Stealth Good" in titles(client.get("/explore").text)


def test_a_rated_game_stays_in_explore_with_its_rating_shown(client, scenario):
    post(client, "/game/40/rate", rating="3.5")
    html = client.get("/explore").text
    assert "Pool Stealth Good" in html
    assert 'data-rating="3.5"' in html


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


def test_the_ratings_page_lists_ratings_and_reviews_and_points_to_dismissed_games(client, scenario):
    post(client, "/game/20/rate", rating="4", review="a <b>bold</b> review")
    post(client, "/game/21/not-interested", value="1")
    html = client.get("/ratings?view=list").text
    assert "Backlog Stealth" in html and "Backlog Sports" not in html  # 21 is only dismissed: it is in the diary
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>bold</b>" not in html  # reviews are escaped
    assert "Delete rating and review" in html and "/diary?show=not-interested" in html


def test_not_interested_hides_a_game_and_undoing_it_puts_everything_back(client, scenario):
    before = client.get("/taste").text
    post(client, "/game/40/not-interested", value="1")
    assert "Pool Stealth Good" not in titles(client.get("/").text)
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
    assert "play score" in html and "50% is your typical played game" in html
    assert "Your rating is what this game teaches your taste" in html


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


# ---- status, dates and favorites ----


def test_setting_a_status_adds_no_date_and_redirects_back(client, scenario):
    response = post(client, "/game/20/status", status="playing", next="/library?source=owned#g20")
    assert response.status_code == 303
    assert response.headers["location"] == "/library?source=owned#g20"
    entry = diary_entry(20)
    assert (entry.status, entry.started_on, entry.finished_on) == ("playing", None, None)


def test_marking_games_played_does_not_count_them_as_finished_in_any_year(client, scenario):
    post(client, "/game/20/status", status="played")
    post(client, "/game/21/status", status="dropped")
    html = client.get("/stats").text
    assert "<li><span>20" not in html  # no year bar
    assert "1 played games have no finish date" in html


def test_a_status_form_with_dates_saves_them(client, scenario):
    post(client, "/game/20/status", status="played", started_on="2025-01-02", finished_on="2025-02-03")
    entry = diary_entry(20)
    assert (entry.started_on.isoformat(), entry.finished_on.isoformat()) == ("2025-01-02", "2025-02-03")


def test_the_quick_status_picker_does_not_wipe_the_dates(client, scenario):
    post(client, "/game/20/status", status="played", started_on="2025-01-02", finished_on="2025-02-03")
    post(client, "/game/20/status", status="dropped")  # the library sends no dates
    entry = diary_entry(20)
    assert (entry.status, entry.finished_on.isoformat()) == ("dropped", "2025-02-03")


def test_blank_dates_in_the_form_clear_them(client, scenario):
    post(client, "/game/20/status", status="played", started_on="2025-01-02", finished_on="2025-02-03")
    post(client, "/game/20/status", status="played", started_on="", finished_on="")
    entry = diary_entry(20)
    assert (entry.started_on, entry.finished_on) == (None, None)


def test_an_empty_status_clears_it(client, scenario):
    post(client, "/game/20/status", status="played")
    post(client, "/game/20/status", status="")
    assert diary_entry(20) is None


@pytest.mark.parametrize(
    "form",
    [
        {"status": "bogus"},
        {"status": "played", "finished_on": "yesterday"},
        {"status": "played", "started_on": "2025-05-01", "finished_on": "2025-02-03"},
    ],
)
def test_bad_status_forms_are_rejected_and_save_nothing(client, scenario, form):
    assert post(client, "/game/20/status", **form).status_code == 400
    assert diary_entry(20) is None


def test_a_status_for_a_game_that_does_not_exist_is_an_error(client, scenario):
    assert post(client, "/game/999999/status", status="played").status_code == 400


def test_favoriting_and_unfavoriting(client, scenario):
    assert post(client, "/game/20/favorite", value="1").status_code == 303
    assert diary_entry(20).favorite is True
    post(client, "/game/20/favorite", value="0")
    assert diary_entry(20) is None
    assert post(client, "/game/999999/favorite", value="1").status_code == 404


def test_the_game_page_shows_the_status_dates_and_favorite(client, scenario):
    post(client, "/game/20/status", status="dropped", started_on="2025-01-02", finished_on="2025-02-03")
    post(client, "/game/20/favorite", value="1")
    html = client.get("/game/20").text
    assert re.search(r'<option value="dropped" selected', html)
    assert 'value="2025-01-02"' in html and 'value="2025-02-03"' in html
    assert "One of your <strong>favorites</strong>" in html and "they don't change your" in html


def test_the_library_row_has_a_status_picker_with_the_current_status(client, scenario):
    post(client, "/game/20/status", status="playing")
    row = re.search(r'<tr id="g20".*?</tr>', client.get("/library").text, re.S).group(0)
    assert re.search(r'<option value="playing" selected', row)


def test_the_library_can_be_filtered_by_status(client, scenario):
    post(client, "/game/20/status", status="played")
    post(client, "/game/21/status", status="dropped")
    only_played = client.get("/library?status=played").text
    assert re.findall(r'<tr id="g(\d+)"', only_played) == ["20"]
    untracked = re.findall(r'<tr id="g(\d+)"', client.get("/library?status=none").text)
    assert "20" not in untracked and "21" not in untracked and len(untracked) > 0
    assert 'value="played" selected' in only_played


def test_an_unknown_status_filter_is_ignored(client, scenario):
    everything = re.findall(r'<tr id="g(\d+)"', client.get("/library").text)
    assert re.findall(r'<tr id="g(\d+)"', client.get("/library?status=nonsense").text) == everything


def test_the_library_pager_keeps_the_status_filter(client, session):
    for i in range(100, 150):
        add_game(session, i, f"Game {i}", tags={"Stealth": 100}, minutes=600)
        diary.set_status(session, i, "played")
    html = client.get("/library?status=played").text
    assert "status=played" in re.search(r'class="pager".*?</p>', html, re.S).group(0)


def test_the_stats_page_shows_your_numbers(client, scenario):
    post(client, "/game/20/status", status="played", finished_on="2025-06-01")
    post(client, "/game/21/status", status="played", finished_on="2025-07-01")
    html = client.get("/stats").text
    assert "games in your library" in html and "10 owned, 1 family" in html
    assert "<li><span>2025 <small>2</small></span>" in html
    assert "average 3.00" in html  # the scenario's ratings are 5 and 1


# ---- saving without a reload (static/inline.js) ----


def test_small_edits_are_marked_to_save_in_place(client, scenario):
    home = client.get("/").text
    assert '<script src="/static/inline.js" defer>' in home
    assert re.search(r'action="/game/\d+/rate" data-inline\s', home)
    assert '<script src="/static/stars.js" defer>' in home
    assert re.search(r'action="/game/\d+/not-interested" data-inline="hide">', home)
    library = client.get("/library").text
    assert "this.form.submit()" not in library and library.count("data-inline") >= 3  # hours, rating, status
    assert client.get("/static/inline.js").status_code == 200


def test_a_marked_form_still_works_without_the_script(client, scenario):
    response = post(client, "/game/20/rate", rating="4", next="/library#g20")
    assert response.status_code == 303 and response.headers["location"] == "/library#g20"


# ---- library columns ----


def library_names(html):
    return re.findall(r'<a href="/game/\d+">([^<]+)</a>\s*</div>', html)


def test_clicking_a_column_sorts_by_it_and_clicking_again_turns_it_round(client, scenario):
    html = client.get("/library").text
    assert 'aria-sort="descending"' in html and "/library?" in html  # most played first by default
    by_name = client.get("/library?sort=name").text
    names = library_names(by_name)
    assert names == sorted(names, key=str.lower) and "dir=desc" in by_name  # the Game column now offers Z to A
    assert library_names(client.get("/library?sort=name&dir=desc").text) == names[::-1]


def test_unrated_games_stay_last_whichever_way_ratings_are_sorted(client, scenario):
    post(client, "/game/20/rate", rating="2")
    post(client, "/game/21/rate", rating="4.5")
    for direction in ("desc", "asc"):
        names = library_names(client.get(f"/library?sort=rating&dir={direction}").text)
        sports, stealth, unrated = (names.index(n) for n in ("Backlog Sports", "Backlog Stealth", "Family Stealth"))
        assert (sports < stealth) == (direction == "desc")  # 4.5 above 2 from high to low, below it from low to high
        assert unrated > max(sports, stealth)


def test_a_sort_the_library_does_not_know_falls_back(client, scenario):
    assert client.get("/library?sort=banana&dir=sideways").status_code == 200


# ---- my ratings ----


def test_my_ratings_shows_posters_and_filters_by_rating_decade_and_tag(client, session, scenario):
    post(client, "/game/20/rate", rating="4", review="good")
    post(client, "/game/21/rate", rating="2")
    html = client.get("/ratings").text
    assert "library_600x900.jpg" in html and 'class="posters"' in html
    assert "\u2605\u2605\u2605\u2605" in html or "★★★★" in html
    four = client.get("/ratings?rating=4").text
    assert "Backlog Stealth" in four and "Backlog Sports" not in four and "Clear filters" in four
    assert client.get("/ratings?rating=9&decade=abc&tag=nonsense").status_code == 200
    stealth = client.get("/ratings?tag=Stealth").text
    assert "Backlog Stealth" in stealth and "Backlog Sports" not in stealth
    assert "Backlog Stealth" not in client.get("/ratings?tag=-Stealth").text  # a minus leaves it out


# ---- the star control ----


def test_each_star_has_two_halves_and_a_way_to_remove_the_rating(client, scenario):
    html = client.get("/game/1").text
    for value in ("0.5", "1", "2.5", "4.5", "5"):
        assert f'name="rating" value="{value}"' in html
    assert 'class="star-clear"' in html and 'class="starrate large"' in html


def test_saving_a_review_on_its_own_keeps_the_rating(client, scenario):
    post(client, "/game/20/rate", rating="4")
    post(client, "/game/20/rate", review="still good")  # the review form has no rating field
    entry = diary_entry(20)
    assert (entry.rating, entry.review) == (4.0, "still good")


def test_my_ratings_rates_from_under_each_poster(client, scenario):
    post(client, "/game/20/rate", rating="2.5")
    html = client.get("/ratings").text
    assert 'class="starrate small"' in html and 'data-rating="2.5"' in html
