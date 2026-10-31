"""What the taste profile learns from: your ratings, how you play, or both."""

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.db import SessionLocal
from shelflife.recommend import explain, recommend, score_all, taste_profile
from shelflife.settings import DEFAULT_TASTE_MODE, TASTE_MODES, get_taste_mode, set_taste_mode
from shelflife.models import Setting
from shelflife.web.app import app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}


@pytest.fixture
def library(session):
    """Three kinds of played game, each with its own tag, plus fillers so tags have rarity:
    one you rated 5 but barely played, one you played a great deal but did not rate,
    one you rated 1 and played a great deal, and one you rated 5 and played a great deal."""
    add_game(session, 1, "Loved, barely played", tags={"Cozy": 100}, minutes=600)
    add_game(session, 2, "Played a great deal", tags={"Grind": 100}, minutes=1000 * 60, achievements=(10, 10), rarest=2.0)
    add_game(session, 3, "Hated, played a great deal", tags={"Gloom": 100}, minutes=1000 * 60, achievements=(10, 10), rarest=2.0)
    add_game(session, 20, "Loved, played a great deal", tags={"Saga": 100}, minutes=1000 * 60, achievements=(10, 10), rarest=2.0)
    for i, tag in enumerate(["Puzzle", "Racing", "Farming", "Chess", "Platformer", "Horror"], start=10):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)
    return {1: 5.0, 3: 1.0, 20: 5.0}


def profile(session, ratings, mode, not_interested=None):
    set_taste_mode(session, mode)
    return taste_profile(session, ratings, not_interested or {}).profile


# ---- the setting ----


def test_the_default_is_the_mix(session):
    assert get_taste_mode(session) == DEFAULT_TASTE_MODE == "mix"


@pytest.mark.parametrize("mode", list(TASTE_MODES))
def test_each_mode_can_be_chosen_and_is_remembered(session, mode):
    set_taste_mode(session, mode)
    with SessionLocal() as other:  # a different session: it was saved, not just held in memory
        assert get_taste_mode(other) == mode


def test_changing_the_mode_replaces_it(session):
    set_taste_mode(session, "ratings")
    set_taste_mode(session, "engagement")
    assert session.query(Setting).count() == 1 and get_taste_mode(session) == "engagement"


@pytest.mark.parametrize("bad", ["", "both", "MIX", "ratings "])
def test_an_unknown_mode_is_refused(session, bad):
    with pytest.raises(ValueError):
        set_taste_mode(session, bad)
    assert get_taste_mode(session) == "mix"


def test_a_stored_mode_that_no_longer_exists_falls_back_to_the_default(session):
    session.add(Setting(key="taste_mode", value="telepathy"))
    session.commit()
    assert get_taste_mode(session) == "mix"


# ---- what each mode learns ----


def ratio(p, tag):
    """A tag's weight against Cozy's (the game rated 5 but barely played), so profiles of
    different scales can be compared."""
    return p.get(tag, 0.0) / p["Cozy"]


def test_the_mix_learns_from_both(session, library):
    mix, only = profile(session, library, "mix"), profile(session, library, "ratings")
    assert mix["Cozy"] > 0  # rated 5: the rating counts, even though barely played
    assert mix["Gloom"] < 0  # rated 1: the rating still wins...
    assert ratio(mix, "Gloom") > ratio(only, "Gloom")  # ...but the great deal of play pulls it up a little
    assert mix["Grind"] > 0  # not rated, played a great deal: play teaches it


def test_the_mix_counts_play_for_more_when_the_games_you_play_most_are_the_ones_you_rate_high(session, library):
    before = ratio(profile(session, library, "mix"), "Grind")
    for i in range(30, 36):
        add_game(session, i, f"Loved {i}", tags={"Saga": 100}, minutes=1000 * 60, achievements=(10, 10), rarest=2.0)
        add_game(session, i + 10, f"Disliked {i}", tags={"Dull": 100}, minutes=70)
    rated = {**library, **{i: 5.0 for i in range(30, 36)}, **{i + 10: 1.5 for i in range(30, 36)}}
    assert ratio(profile(session, rated, "mix"), "Grind") > before


def test_ratings_only_learns_nothing_from_play(session, library):
    p = profile(session, library, "ratings")
    assert p["Cozy"] > 0 and p.get("Gloom", 0) < 0  # what you said
    assert p.get("Grind", 0) == 0  # a great deal of play, no rating: nothing


def test_engagement_only_ignores_what_you_said(session, library):
    p = profile(session, library, "engagement")
    assert p == profile(session, {}, "engagement")  # the same whatever your ratings
    assert p["Grind"] > 0 and p["Gloom"] > 0  # played a great deal: liked, as far as play can tell
    assert p["Cozy"] < 0  # played far less than your usual game: a mild "not for me"


def test_the_three_modes_give_three_different_profiles(session, library):
    profiles = [profile(session, library, m) for m in ("mix", "ratings", "engagement")]
    assert len({tuple(sorted((k, round(v, 6)) for k, v in p.items())) for p in profiles}) == 3


def test_a_favorite_counts_in_the_mix_only(session, library):
    add_game(session, 4, "Favorite", tags={"Epic": 100}, minutes=60)
    without = {m: profile(session, library, m) for m in ("ratings", "engagement")}
    diary.set_favorite(session, 4, True)
    assert profile(session, library, "mix")["Epic"] > 0
    assert profile(session, library, "ratings") == without["ratings"]  # only star ratings count there
    assert profile(session, library, "engagement") == without["engagement"]


def test_not_interested_teaches_a_dislike_in_the_mix_only(session, library):
    add_game(session, 5, "Dismissed", tags={"Grim": 100}, minutes=0)
    dismissed = {5: "Dismissed"}
    assert profile(session, library, "mix", dismissed)["Grim"] < 0
    assert profile(session, library, "ratings", dismissed).get("Grim", 0) == 0
    assert profile(session, library, "engagement", dismissed).get("Grim", 0) == 0


# ---- what does not depend on the mode ----


@pytest.mark.parametrize("mode", list(TASTE_MODES))
def test_a_rated_game_is_never_a_backlog_candidate_in_any_mode(session, library, mode):
    add_game(session, 6, "Rated, never opened", tags={"Cozy": 100}, minutes=0)
    ratings = {**library, 6: 4.0}
    set_taste_mode(session, mode)
    assert 6 not in score_all(session, ratings).backlog


@pytest.mark.parametrize("mode", list(TASTE_MODES))
def test_a_dismissed_game_stays_hidden_in_every_mode(session, library, mode):
    add_game(session, 7, "Hide me", tags={"Cozy": 100}, minutes=0)
    set_taste_mode(session, mode)
    names = [r.game.name for r in recommend(session, library, {7: "Hide me"}, limit=50).backlog]
    assert "Hide me" not in names


def test_recommendations_follow_the_mode(session, library):
    add_game(session, 8, "Cozy candidate", tags={"Cozy": 100}, minutes=0)
    add_game(session, 9, "Grind candidate", tags={"Grind": 100}, minutes=0)

    def order(mode):
        set_taste_mode(session, mode)
        return [r.game.name for r in recommend(session, library, limit=50).backlog if r.score > 0]

    assert order("ratings") == ["Cozy candidate"]  # play is ignored, so Grind teaches nothing
    assert order("engagement") == ["Grind candidate"]  # ratings are ignored, so Cozy teaches nothing
    assert set(order("mix")) == {"Cozy candidate", "Grind candidate"}


def test_the_reason_never_says_rated_when_ratings_are_ignored(session, library):
    add_game(session, 8, "Cozy candidate", tags={"Cozy": 100}, minutes=0)
    add_game(session, 9, "Gloom candidate", tags={"Gloom": 100}, minutes=0)
    set_taste_mode(session, "mix")
    assert "rated 5" in " ".join(explain(session, library, {}, 8).because)
    # You rated game 3 one star but played it a great deal. Counting only play, it pulls
    # Gloom games up, and the reason must describe that as play, not as a rating.
    set_taste_mode(session, "engagement")
    reason = " ".join(explain(session, library, {}, 9).because)
    assert "Hated, played a great deal" in reason and "rated" not in reason and "h played" in reason


def test_the_counts_say_how_many_games_taught_what(session, library):
    set_taste_mode(session, "mix")
    mix = taste_profile(session, library, {})
    assert (mix.explicit_count, mix.implicit_count) == (3, 7)  # three rated games; seven more judged by play alone
    set_taste_mode(session, "ratings")
    only = taste_profile(session, library, {})
    assert (only.explicit_count, only.implicit_count) == (3, 0)
    set_taste_mode(session, "engagement")
    eng = taste_profile(session, library, {})
    assert (eng.explicit_count, eng.implicit_count) == (0, 10)  # every game you played


# ---- the page ----


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def test_the_taste_page_offers_the_three_modes_with_the_current_one_chosen(client):
    html = client.get("/taste").text
    for label in TASTE_MODES.values():
        assert label in html
    assert html.count('type="radio"') == 3
    assert 'value="mix" checked' in html and "(default)" in html


def test_choosing_a_mode_saves_it_and_returns_to_the_page(client, session):
    response = post(client, "/taste/mode", mode="ratings")
    assert response.status_code == 303 and response.headers["location"] == "/taste#mode"
    assert get_taste_mode(session) == "ratings"
    assert 'value="ratings" checked' in client.get("/taste").text


def test_a_bad_mode_is_rejected(client, session):
    assert post(client, "/taste/mode", mode="vibes").status_code == 400
    assert post(client, "/taste/mode").status_code == 400
    assert get_taste_mode(session) == "mix"


def test_the_mode_cannot_be_changed_from_another_website(client, session):
    response = client.post("/taste/mode", data={"mode": "ratings"}, headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert response.status_code == 403 and get_taste_mode(session) == "mix"


def test_the_recommendations_page_says_which_mode_is_on(client, session):
    assert "Taste mode: <strong>Mix of both</strong>" in client.get("/").text
    post(client, "/taste/mode", mode="engagement")
    assert "Taste mode: <strong>How you play only</strong>" in client.get("/").text


def test_a_mode_with_nothing_to_learn_from_says_so_instead_of_pretending(client, session):
    add_game(session, 1, "Played a lot", tags={"Grind": 100}, minutes=1000 * 60, achievements=(10, 10), rarest=2.0)
    for i, tag in enumerate(["Puzzle", "Racing", "Farming"], start=10):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)
    post(client, "/taste/mode", mode="ratings")  # no ratings in this diary
    for page in ("/taste", "/"):
        assert "nothing to learn from yet" in client.get(page).text.lower()
    post(client, "/taste/mode", mode="mix")  # the play counts here
    assert "nothing to learn from" not in client.get("/taste").text.lower()
    assert "nothing to learn from" not in client.get("/").text.lower()


def test_a_mode_that_has_data_shows_no_warning(client, session):
    add_game(session, 1, "Rated", tags={"Cozy": 100}, minutes=600)
    diary.save_rating(session, 1, 5.0)
    post(client, "/taste/mode", mode="ratings")
    assert "nothing to learn from" not in client.get("/taste").text.lower()
