"""What teaches your taste, and nothing else: star ratings (a low one only against close
look-alikes), "not interested" (a weaker low rating) and, if you choose, games you played but didn't
rate (a likely like, as sure as your play). Favorites, statuses and the wishlist never do."""

import pytest
from fastapi.testclient import TestClient

from shelflife import diary
from shelflife.recommend import _build_model, explain, score_all, taste_profile
from shelflife.settings import get_taste_mode, set_taste_mode
from shelflife.web.app import app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}
FILLERS = ["Puzzle", "Racing", "Farming", "Chess", "Platformer", "Sports"]


def fillers(session, start=50):
    """Played games with their own tags, so tags have rarity and play has a typical game."""
    for i, tag in enumerate(FILLERS, start=start):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)


def liking(session, ratings, app_id, not_interested=None):
    return score_all(session, ratings, not_interested or {}).by_id[app_id].similarity


# ---- ratings ----


@pytest.fixture
def horror(session):
    """You rated one horror game low, and love another kind of horror (sci-fi)."""
    fillers(session)
    add_game(session, 1, "Exit Corridor", tags={"Horror": 100, "Walking Simulator": 80, "Puzzle": 30}, minutes=90)
    add_game(session, 2, "Space Routine", tags={"Horror": 100, "Sci-fi": 90, "Atmospheric": 60}, minutes=600)
    add_game(session, 10, "Exit Corridor 2", tags={"Horror": 100, "Walking Simulator": 80, "Puzzle": 35}, owned=False, reviews=(900, 100))
    add_game(session, 11, "Moon Horror", tags={"Horror": 100, "Sci-fi": 85, "Atmospheric": 50}, owned=False, reviews=(900, 100))
    add_game(session, 12, "Haunted Space Station", tags={"Horror": 100, "Sci-fi": 70, "Atmospheric": 40}, owned=False, reviews=(900, 100))
    set_taste_mode(session, "ratings")
    return {1: 1.5, 2: 5.0}


def test_a_liked_game_lifts_games_like_it(session, horror):
    assert liking(session, horror, 11) > 0


def test_a_low_rating_pushes_down_its_look_alikes(session, horror):
    without = liking(session, {2: 5.0}, 10)
    assert liking(session, horror, 10) < without  # the sequel of the game you rated low


def test_a_low_rating_says_little_about_its_genre(session, horror):
    near = liking(session, {2: 5.0}, 10) - liking(session, horror, 10)
    far = liking(session, {2: 5.0}, 12) - liking(session, horror, 12)
    assert near > 3 * far  # the look-alike feels it far more than another horror game
    assert liking(session, horror, 12) > 0  # still lifted by the horror game you love


def test_a_three_star_rating_is_an_ordinary_game_not_a_dislike(session, horror):
    model = _build_model(session, {1: 3.0, 2: 5.0}, {})
    assert model.likes == {1: 0.0, 2: 1.0} and model.dislikes == {}


def test_the_heads_up_names_the_game_you_rated_low(session, horror):
    watch = explain(session, horror, {}, 10).watch
    assert watch == ["Exit Corridor (rated 1.5)"]
    assert explain(session, horror, {}, 11).watch == []


# ---- "not interested" ----

LOVED = {2: 5.0}  # only the sci-fi horror you love; Exit Corridor (1) is not rated
NOT_FOR_ME = {1: "Exit Corridor"}


def test_not_interested_pushes_down_its_look_alikes(session, horror):
    assert liking(session, LOVED, 10, NOT_FOR_ME) < liking(session, LOVED, 10)


def test_not_interested_counts_less_than_a_two_star_rating(session, horror):
    before = liking(session, LOVED, 10)
    by_mark = before - liking(session, LOVED, 10, NOT_FOR_ME)
    by_rating = before - liking(session, LOVED | {1: 2.0}, 10)
    assert 0 < by_mark < by_rating


def test_not_interested_says_little_about_its_genre(session, horror):
    near = liking(session, LOVED, 10) - liking(session, LOVED, 10, NOT_FOR_ME)
    far = liking(session, LOVED, 12) - liking(session, LOVED, 12, NOT_FOR_ME)
    assert near > 3 * far


def test_a_rating_wins_over_not_interested(session, horror):
    assert liking(session, horror, 10, NOT_FOR_ME) == liking(session, horror, 10)
    model = _build_model(session, horror, NOT_FOR_ME)
    assert model.dismissed == set() and 1 in model.dislikes


def test_a_game_you_played_then_dismissed_is_not_learned_as_liked(session, horror):
    set_taste_mode(session, "both")
    model = _build_model(session, LOVED, NOT_FOR_ME)
    assert 1 in model.dismissed and 1 not in model.played


def test_the_heads_up_names_a_game_you_are_not_interested_in(session, horror):
    assert explain(session, LOVED, NOT_FOR_ME, 10).watch == ["Exit Corridor (not interested)"]


def test_the_taste_page_lists_the_games_you_are_not_interested_in(session, horror):
    assert [g.name for g in taste_profile(session, LOVED, NOT_FOR_ME).dismissed] == ["Exit Corridor"]


def test_the_taste_page_lists_the_games_you_rated_low(session, horror):
    taste = taste_profile(session, horror, {})
    assert [(g.name, r) for g, r in taste.disliked] == [("Exit Corridor", 1.5)]
    assert all(v >= 0 for v in taste.profile.values())  # dislikes never become "leans away" tags


# ---- games you played but didn't rate ----


@pytest.fixture
def played(session):
    fillers(session)
    add_game(session, 1, "Loved", tags={"Zeta": 100}, minutes=3000)
    add_game(session, 2, "Played a lot", tags={"Eta": 100}, minutes=60000, achievements=(30, 30), rarest=1.0)
    add_game(session, 3, "Barely played", tags={"Kappa": 100}, minutes=70)
    add_game(session, 20, "Like the played one", tags={"Eta": 100, "Iota": 20}, owned=False, reviews=(900, 100))
    add_game(session, 21, "Like the barely played one", tags={"Kappa": 100, "Iota": 20}, owned=False, reviews=(900, 100))
    add_game(session, 22, "Unlike anything", tags={"Omega": 100}, owned=False, reviews=(900, 100))
    return {1: 5.0}


def test_learning_from_play_is_on_by_default_and_remembered(session):
    assert get_taste_mode(session) == "both"
    set_taste_mode(session, "ratings")
    assert get_taste_mode(session) == "ratings"


def test_a_game_you_played_a_lot_counts_as_a_likely_like(session, played):
    assert liking(session, played, 20) > liking(session, played, 22)


def test_more_play_counts_more_and_little_play_never_counts_against(session, played):
    assert liking(session, played, 20) > liking(session, played, 21)
    assert liking(session, played, 21) >= liking(session, played, 22)  # barely played: no dislike


def test_with_the_switch_off_play_teaches_nothing(session, played):
    set_taste_mode(session, "ratings")
    assert liking(session, played, 20) == pytest.approx(liking(session, played, 22))
    model = _build_model(session, played, {})
    assert model.implicit == 0 and model.explicit == 1


def test_a_rated_game_teaches_through_its_rating_not_its_play(session, played):
    model = _build_model(session, {1: 5.0, 2: 1.0}, {})
    assert 2 in model.dislikes and 2 not in model.played  # played a lot, rated 1: the rating speaks


# ---- play only ----


def test_play_only_ignores_ratings_and_learns_every_played_game_from_its_play(session, played):
    model = _build_model(session, {1: 5.0, 2: 1.0}, {}, mode="play")
    assert model.likes == {} and model.dislikes == {} and model.explicit == 0
    assert {1, 2, 3} <= set(model.played)  # rated or not, every played game counts by its play


def test_in_play_only_a_rating_changes_nothing(session, played):
    set_taste_mode(session, "play")
    assert liking(session, {2: 1.0}, 20) == pytest.approx(liking(session, {}, 20))
    assert liking(session, played, 20) > liking(session, played, 22)  # still learned from play


def test_in_play_only_a_rated_game_is_still_not_recommended(session, played):
    set_taste_mode(session, "play")
    add_game(session, 23, "Rated elsewhere", tags={"Eta": 100}, owned=False, reviews=(900, 100))
    assert 23 in score_all(session, played).discover
    assert 23 not in score_all(session, played | {23: 4.0}).discover


def test_not_interested_still_counts_in_play_only(session, horror):
    set_taste_mode(session, "play")
    assert liking(session, {}, 10, NOT_FOR_ME) < liking(session, {}, 10)


def test_the_old_switch_carries_over_as_ratings_only(session):
    from shelflife.models import Setting

    session.add(Setting(key="learn_from_play", value="0"))
    session.commit()
    assert get_taste_mode(session) == "ratings"
    set_taste_mode(session, "play")
    assert get_taste_mode(session) == "play"
    with pytest.raises(ValueError):
        set_taste_mode(session, "nonsense")


def test_the_counts_say_how_many_games_taught_what(session, played):
    taste = taste_profile(session, played, {})
    assert (taste.explicit_count, taste.implicit_count) == (1, 2 + len(FILLERS))


# ---- what never teaches your taste ----


@pytest.fixture
def library(session):
    fillers(session)
    add_game(session, 1, "Loved", tags={"Zeta": 100}, minutes=3000)
    add_game(session, 2, "Other", tags={"Eta": 100}, minutes=600)
    add_game(session, 3, "Elsewhere", tags={"Theta": 100})
    add_game(session, 20, "Pool game", tags={"Zeta": 100, "Iota": 20}, owned=False, reviews=(900, 100))
    return {1: 5.0}


@pytest.mark.parametrize(
    "action",
    [
        lambda s: diary.set_favorite(s, 3, True),
        lambda s: diary.set_status(s, 2, "dropped"),
        lambda s: diary.set_status(s, 3, "played"),
    ],
    ids=["favorite", "dropped", "played status"],
)
def test_favorites_and_statuses_never_change_the_taste(session, library, action):
    before = taste_profile(session, library, {}).profile
    action(session)
    assert taste_profile(session, library, {}).profile == before


def test_not_interested_hides_the_game_and_leaves_the_tag_profile_alone(session, library):
    before = taste_profile(session, library, {}).profile
    marked = {20: "Pool game"}
    assert taste_profile(session, library, marked).profile == before
    assert 20 not in score_all(session, library, marked).discover


def test_a_favorite_is_in_your_diary_so_it_is_not_recommended(session, library):
    assert 3 in score_all(session, library).backlog
    diary.set_favorite(session, 3, True)
    assert 3 not in score_all(session, library).backlog
    diary.set_favorite(session, 20, True)
    assert 20 not in score_all(session, library).discover


# ---- the switch on the page ----


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_the_taste_page_offers_the_three_modes_with_the_current_one_picked(client, session):
    html = client.get("/taste").text
    assert 'action="/taste/mode"' in html
    assert all(f'value="{m}"' in html for m in ("both", "ratings", "play"))
    assert 'value="both" checked' in html and "Ratings and play" in html


@pytest.mark.parametrize("mode, says", [("ratings", "(ratings only)"), ("play", "(play only)"), ("both", "games you played")])
def test_picking_a_mode_is_remembered_and_shown(client, session, mode, says):
    client.post("/taste/mode", data={"mode": mode}, headers=SAME_SITE)
    assert get_taste_mode(session) == mode
    assert says in client.get("/").text
    assert f'value="{mode}" checked' in client.get("/taste").text


def test_an_unknown_mode_changes_nothing(client, session):
    client.post("/taste/mode", data={"mode": "nonsense"}, headers=SAME_SITE)
    assert get_taste_mode(session) == "both"


def test_with_nothing_to_learn_from_the_pages_say_so(client, session):
    add_game(session, 1, "Played a lot", tags={"Grind": 100}, minutes=60000)
    client.post("/taste/mode", data={"mode": "ratings"}, headers=SAME_SITE)
    for page in ("/taste", "/"):
        assert "nothing to learn from yet" in client.get(page).text.lower()
    client.post("/taste/mode", data={"mode": "both"}, headers=SAME_SITE)
    assert "nothing to learn from" not in client.get("/taste").text.lower()


def test_the_game_page_says_what_this_game_teaches(client, session):
    fillers(session)
    add_game(session, 1, "Rated low", tags={"Zeta": 100}, minutes=600)
    add_game(session, 2, "Only played", tags={"Eta": 100}, minutes=600)
    diary.save_rating(session, 1, 1.5)
    assert "pushes down only games very like it" in client.get("/game/1").text
    assert "counts as a game you probably liked" in client.get("/game/2").text
    set_taste_mode(session, "ratings")
    assert "don't teach your taste" in client.get("/game/2").text
