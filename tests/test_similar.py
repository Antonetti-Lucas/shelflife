"""Games like one game: the similarity, the Explore view, the search and the game page."""

import re
import threading

import pytest
import requests
from fastapi.testclient import TestClient

from shelflife import diary, discovery, store
from shelflife.discover_job import DiscoverJob, JobSnapshot, summarize
from shelflife.discovery import FindResult, find_games
from shelflife.models import Game
from shelflife.similar import MIN_SIMILARITY, SEARCH_TAGS, search_tags, similar_to, tag_idf
from shelflife.store import SpyDetails, SpyListing
from shelflife.tags import tag_vector
from shelflife.web import app as web_app
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


def names_on(html):
    """The game cards' titles (the heading also links the reference game)."""
    return re.findall(r'<h3><a href="/game/\d+">([^<]+)</a></h3>', html)


@pytest.fixture
def world(session):
    """A reference game and a few unowned games, from very close to unrelated."""
    ref = add_game(session, 1, "Reference", tags={"Stealth": 100, "Assassin": 90, "Comedy": 40}, minutes=3000)
    for i, tag in enumerate(["Puzzle", "Racing", "Farming", "Chess", "Platformer", "Horror"], start=10):
        add_game(session, i, f"Filler {tag}", tags={tag: 100}, minutes=600)  # makes tags rarer
    add_game(session, 20, "Close", tags={"Stealth": 100, "Assassin": 85, "Comedy": 30}, owned=False, reviews=(900, 100))
    add_game(session, 21, "Half", tags={"Stealth": 100, "Racing": 90}, owned=False, reviews=(900, 100))
    add_game(session, 22, "Far", tags={"Sports": 100, "Racing": 60}, owned=False, reviews=(900, 100))
    add_game(session, 23, "Untagged", owned=False, reviews=(900, 100))
    return ref


# ---- the similarity ----


def test_games_are_ranked_from_the_closest_to_the_furthest(session, world):
    idf = tag_idf(session)
    unowned = [g for g in session.query(Game).all() if g.library_entry is None]
    ranked = similar_to(world, unowned, idf)
    assert [r.game.name for r in ranked] == ["Close", "Half", "Far"]
    assert ranked[0].similarity > ranked[1].similarity > ranked[2].similarity == 0


def test_the_game_itself_and_games_without_tags_are_left_out(session, world):
    idf = tag_idf(session)
    everything = session.query(Game).all()
    names = [r.game.name for r in similar_to(world, everything, idf)]
    assert "Reference" not in names and "Untagged" not in names


def test_each_result_names_the_tags_it_shares_with_the_game(session, world):
    idf = tag_idf(session)
    close = next(r for r in similar_to(world, session.query(Game).all(), idf) if r.game.name == "Close")
    assert set(close.tags) <= {"Stealth", "Assassin", "Comedy"} and len(close.tags) >= 2
    assert close.because == [] and close.watch == []


def test_the_review_quality_breaks_ties_between_equally_similar_games(session):
    ref = add_game(session, 1, "Ref", tags={"Stealth": 100})
    add_game(session, 2, "Well liked", tags={"Stealth": 100}, owned=False, reviews=(9000, 100))
    add_game(session, 3, "Disliked", tags={"Stealth": 100}, owned=False, reviews=(300, 300))
    ranked = similar_to(ref, session.query(Game).all(), tag_idf(session))
    assert [r.game.name for r in ranked] == ["Well liked", "Disliked"]


def test_the_search_tags_are_the_ones_that_most_set_the_game_apart(session, world):
    tags = search_tags(world, tag_idf(session))
    assert len(tags) <= SEARCH_TAGS and tags[0] in {"Stealth", "Assassin"}
    assert "Racing" not in tags


def test_a_game_with_no_tags_has_nothing_to_search_for(session):
    game = add_game(session, 1, "Bare")
    assert search_tags(game, tag_idf(session)) == []


# ---- the Explore view ----


@pytest.fixture
def client(monkeypatch):
    held = []
    job = web_app.discover_job
    monkeypatch.setattr(job, "_snapshot", JobSnapshot())
    monkeypatch.setattr(job, "_spawn", held.append)
    monkeypatch.setattr(job, "_stop", threading.Event())
    with TestClient(web_app.app) as c:
        c.held = held
        yield c


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def test_explore_like_a_game_lists_the_closest_first_and_says_so(client, session, world):
    html = client.get("/explore?like=1").text
    assert names_on(html) == ["Close", "Half", "Far"]
    assert "Games like Reference" in html and "% similar" in html and "to Reference" in html
    assert 'name="like" value="1"' in html


def test_like_mode_ranks_by_likeness_not_by_your_taste(client, session, world):
    # You love racing games, so plain Explore puts the racing ones first; "like Reference"
    # must still put the stealth game that resembles it first.
    diary.save_rating(session, 11, 5.0)  # Filler Racing
    plain = names_on(client.get("/explore").text)
    like = names_on(client.get("/explore?like=1").text)
    assert plain[0] != "Close" and like[0] == "Close"
    assert like == ["Close", "Half", "Far"]


def test_explore_without_like_is_still_the_taste_ranking(client, session, world):
    html = client.get("/explore").text
    assert "Explore Steam" in html and "% similar" not in html and "% match" in html


def test_like_mode_only_offers_the_sorts_that_make_sense(client, session, world):
    like = client.get("/explore?like=1").text
    assert 'value="similar" selected' in like and 'value="fit"' not in like
    plain = client.get("/explore").text
    assert 'value="fit" selected' in plain and 'value="similar"' not in plain


def test_like_mode_keeps_the_filters_and_the_other_sorts(client, session, world):
    assert names_on(client.get("/explore?like=1&tag=Racing").text) == ["Half", "Far"]
    assert names_on(client.get("/explore?like=1&q=clo").text) == ["Close"]
    assert names_on(client.get("/explore?like=1&sort=name").text) == ["Close", "Far", "Half"]


def test_like_mode_never_lists_the_game_or_owned_games(client, session, world):
    names = names_on(client.get("/explore?like=1").text)
    assert "Reference" not in names and not any(n.startswith("Filler") for n in names)


def test_dismissed_games_are_left_out_of_like_mode_too(client, session, world):
    post(client, "/game/20/not-interested", value="1")
    assert "Close" not in names_on(client.get("/explore?like=1").text)


@pytest.mark.parametrize("like", ["", "abc", "999999", "23", "-1"])
def test_a_bad_like_value_falls_back_to_the_normal_explore(client, session, world, like):
    # 23 is a game with no tags: nothing to compare against
    html = client.get(f"/explore?like={like}").text
    assert "Explore Steam" in html and "Games like" not in html


def test_like_mode_pages_keep_the_game(client, session, world):
    for i in range(100, 130):
        add_game(session, i, f"More {i}", tags={"Stealth": 100}, owned=False, reviews=(900, 100))
    html = client.get("/explore?like=1").text
    assert "like=1" in re.search(r'class="pager".*?</p>', html, re.S).group(0)


# ---- the game page ----


def test_every_game_with_tags_offers_similar_games(client, session, world):
    html = client.get("/game/1").text
    assert 'href="/explore?like=1"' in html and "Games like this in Explore" in html
    assert 'action="/explore/find"' in html and 'name="like" value="1"' in html
    assert "Stealth" in html  # the tags the search will use are shown


def test_a_game_without_tags_does_not_offer_it(client, session, world):
    html = client.get("/game/23").text
    assert "Similar games" not in html and "like=23" not in html


# ---- the search for new games like one ----


def start_calls(monkeypatch):
    calls = []

    def start(tags, count, min_reviews, like=None):
        calls.append((tags, count, min_reviews, like))
        return True

    monkeypatch.setattr(web_app.discover_job, "start", start)
    return calls


def test_the_button_searches_the_games_own_tags_and_remembers_which_game(client, session, world, monkeypatch):
    calls = start_calls(monkeypatch)
    response = post(client, "/explore/find", like="1", count="10")
    assert response.status_code == 303 and response.headers["location"] == "/explore?like=1#find"
    tags, count, min_reviews, like = calls[0]
    assert tags == search_tags(world, tag_idf(session)) and count == 10 and like == (1, "Reference")


def test_typed_tags_are_ignored_when_searching_for_games_like_one(client, session, world, monkeypatch):
    calls = start_calls(monkeypatch)
    post(client, "/explore/find", like="1", tags="Racing, Sports")
    assert "Racing" not in calls[0][0]


@pytest.mark.parametrize("like", ["23", "999999", "abc"])
def test_a_game_that_cannot_be_compared_says_why(client, session, world, monkeypatch, like):
    calls = start_calls(monkeypatch)
    post(client, "/explore/find", like=like)
    assert calls == []
    assert "no tags yet" in client.get("/explore").text


def test_a_finished_like_search_links_to_its_results(client, session, world, fakes):
    lists, details, enriched = fakes
    lists["Stealth"] = [SpyListing(300, "New Stealth Game", 900, 100)]
    details[300] = SpyDetails({"Stealth": 100, "Assassin": 90, "Comedy": 40}, 900, 100)
    post(client, "/explore/find", like="1", count="10")
    client.held[0]()
    plain = client.get("/explore").text
    assert 'href="/explore?like=1"' in plain and "See games like Reference" in plain
    assert "See games like" not in client.get("/explore?like=1").text  # already looking at them
    assert "New Stealth Game" in names_on(client.get("/explore?like=1").text)


# ---- keeping only close matches ----


@pytest.fixture
def fakes(monkeypatch):
    lists, details, enriched = {}, {}, []
    monkeypatch.setattr(store, "fetch_spy_tag_list", lambda tag, timeout=90: lists.get(tag, []))
    monkeypatch.setattr(store, "fetch_spy_details", lambda app_id, timeout=15: details[app_id])

    def fake_enrich(game, spy=None):
        enriched.append(game.steam_app_id)
        game.type = "game"
        game.details_fetched_at = discovery._now()

    monkeypatch.setattr(discovery, "enrich_one", fake_enrich)
    return lists, details, enriched


def about(*names):
    return SpyDetails({n: 1000 - i for i, n in enumerate(names)}, 900, 100)


def test_only_games_close_to_the_reference_are_kept(session, fakes):
    lists, details, enriched = fakes
    idf = {f"Other {i}": 1.0 for i in range(20)} | {"Stealth": 1.0, "Action": 1.0}
    reference = tag_vector({"Stealth": 100, "Action": 60}, idf)
    lists["Stealth"] = [SpyListing(1, "Close", 5000, 0), SpyListing(2, "Far", 4000, 0)]
    details[1] = about("Stealth", "Action")
    # Stealth is in its top 10 (so it passes the tag check) but a minor tag: nothing like the reference.
    details[2] = SpyDetails({**{f"Other {i}": 1000 for i in range(9)}, "Stealth": 200}, 900, 100)
    result = find_games(session, ["Stealth"], 5, like=(reference, idf))
    assert enriched == [1] and (result.added, result.skipped) == (1, 1)
    assert session.get(Game, 2).type == discovery.OFF_TOPIC


def test_without_a_reference_the_same_far_game_is_kept(session, fakes):
    lists, details, enriched = fakes
    lists["Stealth"] = [SpyListing(2, "Far", 4000, 0)]
    details[2] = SpyDetails({**{f"Other {i}": 1000 for i in range(9)}, "Stealth": 200}, 900, 100)
    assert find_games(session, ["Stealth"], 5).added == 1


@pytest.mark.parametrize("votes_for_a, kept", [(400, True), (380, False)])
def test_the_closeness_line_is_exactly_where_it_is_documented(session, fakes, votes_for_a, kept):
    # With weights (votes / top votes) squared, a game {"A": x, "B": 1000} is 'x' weighted by
    # (x/1000)^2 against B at 1, so its cosine to a reference made only of A is about 0.158 at
    # 400 votes and 0.143 at 380: either side of the 0.15 line.
    lists, details, enriched = fakes
    assert MIN_SIMILARITY == 0.15
    idf = {"A": 1.0, "B": 1.0}
    lists["A"] = [SpyListing(1, "Maybe", 5000, 0)]
    details[1] = SpyDetails({"A": votes_for_a, "B": 1000}, 900, 100)
    result = find_games(session, ["A"], 5, like=({"A": 1.0}, idf))
    assert (result.added == 1) is kept


# ---- the job and its messages ----


def test_a_like_search_remembers_the_game_in_its_state_and_its_message(session, fakes, monkeypatch):
    lists, details, enriched = fakes
    for i, tag in enumerate(["Puzzle", "Racing", "Farming", "Chess"], start=10):
        add_game(session, i, f"Filler {tag}", tags={tag: 100})  # so that tags have some rarity
    ref = add_game(session, 1, "Ref", tags={"Stealth": 100, "Action": 60})
    lists["Stealth"] = [SpyListing(7, "New", 5000, 0)]
    details[7] = about("Stealth", "Action")
    job = DiscoverJob(spawn=lambda fn: fn())
    job.start(["Stealth"], 5, 300, like=(1, "Ref"))
    snap = job.snapshot()
    assert snap.state == "done" and (snap.like_id, snap.like_name) == (1, "Ref") and "Added 1 new game" in snap.message


def test_the_running_state_carries_the_game_too():
    held = []
    job = DiscoverJob(spawn=held.append)
    job.start(["A"], 5, 300, like=(1, "Ref"))
    assert job.snapshot().running and job.snapshot().like_name == "Ref"


def test_the_message_for_a_like_search_names_the_game():
    snap = summarize(FindResult(added=2, enriched=2, checked=9, skipped=7), ["Tag"], like=(1, "Hades"))
    assert "not close enough to Hades" in snap.message and snap.like_id == 1
    plain = summarize(FindResult(added=2, enriched=2, checked=9, skipped=7), ["Tag"])
    assert "only mention the tag in passing" in plain.message and plain.like_id is None


def test_a_failed_like_search_still_says_which_game():
    snap = summarize(FindResult(aborted="rate limited."), ["Tag"], like=(1, "Hades"))
    assert snap.failed and snap.like_name == "Hades"
