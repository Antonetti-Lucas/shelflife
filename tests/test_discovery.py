"""Finding new games to explore: who is looked at and kept, the background job and its button."""

import threading

import pytest
import requests
from fastapi.testclient import TestClient

from shelflife import diary, discovery, store
from shelflife.discover_job import DiscoverJob, JobSnapshot, summarize
from shelflife.discovery import (
    MAX_CHECKS_PER_GAME,
    OFF_TOPIC,
    RELEVANT_TOP,
    FindResult,
    find_games,
    known_ids,
    rank_candidates,
    turns,
)
from shelflife.models import Game
from shelflife.store import SpyDetails, SpyListing
from shelflife.throttle import RateLimited
from shelflife.web import app as web_app
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}


def listing(app_id, positive=900, negative=100, name=None):
    return SpyListing(app_id, name or f"Game {app_id}", positive, negative)


def about(*names, extra=0):
    """SteamSpy details for a game whose most voted tags are `names`, in that order."""
    tags = {name: 1000 - i for i, name in enumerate(names)}
    tags.update({f"Filler {i}": 100 - i for i in range(extra)})
    return SpyDetails(tags, 900, 100)


# ---- who is looked at ----


def test_with_nearly_equal_praise_the_game_with_many_more_reviews_ranks_higher():
    big = listing(1, 19400, 600)  # 97% of 20,000
    small = listing(2, 294, 6)  # 98% of 300
    assert [g.app_id for g in rank_candidates([small, big], set())] == [1, 2]


def test_games_you_have_and_obscure_games_are_not_candidates():
    games = [listing(1), listing(2), listing(3, 100, 100), listing(4, 20, 10)]  # 3: 200 reviews, 4: 30
    assert [g.app_id for g in rank_candidates(games, {1})] == [2]  # 300 reviews needed
    assert {g.app_id for g in rank_candidates(games, {1}, min_reviews=100)} == {2, 3}


def test_tags_take_turns_so_each_gets_a_fair_share():
    a = [listing(i, 2000 - i, 0) for i in (1, 2, 3)]
    b = [listing(i, 1500 - i, 0) for i in (11, 12, 13)]
    assert [(t, g.app_id) for t, g in turns({"A": a, "B": b})] == [
        ("A", 1), ("B", 11), ("A", 2), ("B", 12), ("A", 3), ("B", 13),
    ]  # fmt: skip


def test_a_game_listed_under_two_tags_comes_up_once():
    shared = listing(1)
    ids = [g.app_id for _, g in turns({"A": [shared, listing(2)], "B": [shared, listing(3)]})]
    assert sorted(ids) == [1, 2, 3]


def test_a_short_tag_does_not_hold_the_others_back():
    ids = [(t, g.app_id) for t, g in turns({"Tiny": [listing(1)], "Big": [listing(i) for i in range(10, 14)]})]
    assert len(ids) == 5 and ids[0][0] == "Tiny" and sum(1 for t, _ in ids if t == "Tiny") == 1


def test_no_candidates_means_no_turns():
    assert list(turns({})) == [] and list(turns({"A": []})) == []


# ---- fakes: no network ----


@pytest.fixture(autouse=True)
def lists(monkeypatch):
    """Fake SteamSpy tag lists: tag -> listings (or an exception to raise)."""
    data = {}
    calls = []

    def fetch(tag, timeout=90):
        calls.append(tag)
        item = data.get(tag, [])
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(store, "fetch_spy_tag_list", fetch)
    fetch.data, fetch.calls = data, calls
    return fetch


@pytest.fixture(autouse=True)
def details(monkeypatch):
    """Fake SteamSpy game details: app id -> SpyDetails (or an exception). A game not
    set here is about every tag the tests search for."""
    data = {}
    calls = []
    everything = about("Stealth", "RPG", "Tag", "A", "B", "Roguelike")

    def fetch(app_id, timeout=15):
        calls.append(app_id)
        item = data.get(app_id, everything)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(store, "fetch_spy_details", fetch)
    fetch.data, fetch.calls = data, calls
    return fetch


@pytest.fixture(autouse=True)
def enrich(monkeypatch):
    """A stand-in for fetching store details: marks the game as fetched."""
    log, spies, fail = [], {}, {}

    def fake(game, spy=None):
        if game.steam_app_id in fail:
            raise fail[game.steam_app_id]
        log.append(game.steam_app_id)
        spies[game.steam_app_id] = spy
        game.type = "game"
        game.details_fetched_at = discovery._now()

    monkeypatch.setattr(discovery, "enrich_one", fake)
    fake.log, fake.spies, fake.fail = log, spies, fail
    return fake


# ---- finding games end to end ----


def test_finding_games_keeps_them_and_fetches_their_details(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(i, 5000 - i, 0) for i in range(1, 8)]
    lists.data["RPG"] = [listing(i, 4000 - i, 0) for i in range(20, 28)]
    steps = []
    result = find_games(session, ["Stealth", "RPG"], 6, on_progress=lambda *a: steps.append(a))
    assert (result.added, result.enriched, result.failed, result.skipped) == (6, 6, 0, 0)
    assert result.aborted is None and not result.stopped and result.empty_tags == []
    assert session.query(Game).filter(Game.details_fetched_at.is_not(None)).count() == 6
    assert [s[0] for s in steps if s[0] == "lists"] == ["lists", "lists"]
    assert {"check", "details"} <= {s[0] for s in steps} and steps[-1][0] == "done"
    assert any(s[0] == "check" and "Checking Game" in s[3] for s in steps)


def test_a_game_that_only_mentions_the_tag_is_set_aside_and_the_next_one_is_taken(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(i, 5000 - i, 0) for i in range(1, 8)]
    details.data[1] = about(*[f"Other {i}" for i in range(RELEVANT_TOP)], "Stealth")  # 11th: not about it
    details.data[2] = about("Pizza")  # has no Stealth at all
    result = find_games(session, ["Stealth"], 2)
    assert (result.added, result.skipped, result.checked) == (2, 2, 4)
    assert sorted(enrich.log) == [3, 4]
    for off in (1, 2):
        row = session.get(Game, off)
        assert row.type == OFF_TOPIC and row.tags and row.details_fetched_at is not None


def test_the_tag_must_be_among_the_ten_most_voted(session, lists, details, enrich):
    # Written with the literal numbers so changing the rule makes this fail.
    lists.data["Tag"] = [listing(1, 5000, 0), listing(2, 4000, 0)]
    details.data[1] = about(*[f"Other {i}" for i in range(9)], "Tag")  # exactly 10th
    details.data[2] = about(*[f"Other {i}" for i in range(10)], "Tag")  # 11th
    result = find_games(session, ["Tag"], 5)
    assert sorted(enrich.log) == [1] and result.skipped == 1
    assert RELEVANT_TOP == 10


def test_matching_the_searched_tag_ignores_case(session, lists, details, enrich):
    lists.data["roguelike"] = [listing(1)]
    details.data[1] = about("Roguelike")
    assert find_games(session, ["roguelike"], 1).added == 1


def test_a_game_steamspy_has_no_tags_for_is_set_aside(session, lists, details, enrich):
    lists.data["Tag"] = [listing(1, 5000, 0), listing(2, 4000, 0)]
    details.data[1] = SpyDetails({}, 900, 100)
    result = find_games(session, ["Tag"], 1)
    assert enrich.log == [2] and result.skipped == 1


def test_set_aside_games_are_not_fetched_again_by_the_next_search(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(i, 5000 - i, 0) for i in range(1, 6)]
    details.data[1] = about("Pizza")
    find_games(session, ["Stealth"], 1)
    details.calls.clear()
    result = find_games(session, ["Stealth"], 1)
    assert 1 not in details.calls  # remembered, and not a match for Stealth: no new request
    assert result.added == 1 and result.skipped == 1
    assert session.get(Game, 1).type == OFF_TOPIC


def test_a_set_aside_game_is_kept_when_a_later_search_is_about_it_without_asking_again(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(1)]
    details.data[1] = about("Pizza", "Cooking")
    find_games(session, ["Stealth"], 1)
    assert session.get(Game, 1).type == OFF_TOPIC

    lists.data["Pizza"] = [listing(1)]
    details.calls.clear()
    result = find_games(session, ["Pizza"], 1)
    assert result.added == 1 and details.calls == []  # its saved tags were enough
    assert enrich.log == [1] and session.get(Game, 1).type == "game"  # now a normal pool game
    assert enrich.spies[1].tags == about("Pizza", "Cooking").tags  # and no second SteamSpy request


def test_set_aside_games_are_not_counted_as_games_you_have(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(1)]
    details.data[1] = about("Pizza")
    find_games(session, ["Stealth"], 1)
    add_game(session, 2, "Owned")
    assert known_ids(session) == {2}


def test_it_gives_up_looking_after_a_limit_when_nothing_matches(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(i, 100000 - i, 0) for i in range(1, 100)]
    for i in range(1, 100):
        details.data[i] = about("Pizza")
    result = find_games(session, ["Stealth"], 2)
    assert result.added == 0 and result.skipped == 2 * MAX_CHECKS_PER_GAME
    assert len(details.calls) == 2 * MAX_CHECKS_PER_GAME


def test_games_you_already_have_are_never_looked_at(session, lists, details, enrich):
    add_game(session, 1, "Owned")
    add_game(session, 2, "In the pool", owned=False)
    lists.data["Stealth"] = [listing(1, 9000, 0), listing(2, 8000, 0), listing(3, 7000, 0)]
    result = find_games(session, ["Stealth"], 5)
    assert details.calls == [3] and result.added == 1


def test_games_with_too_few_reviews_are_never_looked_at(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(1, 100, 100), listing(2, 900, 100)]
    find_games(session, ["Stealth"], 5)
    assert details.calls == [2]
    find_games(session, ["Stealth"], 5, min_reviews=100)
    assert 1 in details.calls


def test_tags_take_turns_when_finding_games(session, lists, details, enrich):
    lists.data["A"] = [listing(i, 5000 - i, 0) for i in (1, 2, 3, 4)]
    lists.data["B"] = [listing(i, 4000 - i, 0) for i in (11, 12, 13, 14)]
    find_games(session, ["A", "B"], 4)
    assert details.calls == [1, 11, 2, 12]


def test_the_steamspy_details_already_fetched_are_reused_for_the_store_step(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(1)]
    details.data[1] = about("Stealth", "Cats")
    find_games(session, ["Stealth"], 1)
    assert details.calls == [1]  # asked once, in the check
    assert enrich.spies[1] is details.data[1]


def test_a_tag_with_no_games_is_reported_and_the_others_still_work(session, lists, details, enrich):
    lists.data["Stealth"] = [listing(1), listing(2)]
    result = find_games(session, ["Stealth", "Typo"], 5)
    assert result.empty_tags == ["Typo"] and result.added == 2


def test_games_left_over_from_an_interrupted_run_are_finished_too(session, lists, details, enrich):
    add_game(session, 99, "Left over", owned=False)  # in the pool, no details yet
    lists.data["Tag"] = [listing(1), listing(2)]
    result = find_games(session, ["Tag"], 1)
    assert result.added == 1 and result.enriched == 2
    assert 99 in enrich.log and enrich.spies[99] is None


# ---- stopping and failing ----


def test_stopping_keeps_what_was_done_and_says_so(session, lists, details, enrich):
    lists.data["Tag"] = [listing(i, 5000 - i, 0) for i in range(1, 6)]
    result = find_games(session, ["Tag"], 5, should_stop=lambda: len(details.calls) >= 2)
    assert result.stopped and result.added == 2
    assert session.query(Game).filter(Game.type.is_(None)).count() == 2  # kept, details not fetched


def test_stopping_during_the_store_step_keeps_the_finished_games(session, lists, details, enrich):
    lists.data["Tag"] = [listing(i, 5000 - i, 0) for i in range(1, 5)]
    result = find_games(session, ["Tag"], 4, should_stop=lambda: len(enrich.log) >= 2)
    assert result.stopped and result.added == 4 and result.enriched == 2


def test_stopping_before_the_first_lookup_adds_nothing(session, lists, details, enrich):
    lists.data["Tag"] = [listing(1)]
    result = find_games(session, ["Tag"], 5, should_stop=lambda: True)
    assert result.stopped and result.added == 0 and lists.calls == []


def test_a_rate_limited_tag_lookup_adds_nothing_and_says_why(session, lists, details, enrich):
    lists.data["Tag"] = RateLimited("SteamSpy is rate limiting us.")
    result = find_games(session, ["Tag"], 5)
    assert result.added == 0 and "rate limiting" in result.aborted


def test_a_network_error_on_a_tag_lookup_is_reported_not_raised(session, lists, details, enrich):
    lists.data["Tag"] = requests.ConnectionError("down")
    result = find_games(session, ["Tag"], 5)
    assert result.added == 0 and "Could not reach SteamSpy" in result.aborted


def test_rate_limiting_while_reading_game_tags_stops_but_keeps_what_was_found(session, lists, details, enrich):
    lists.data["Tag"] = [listing(i, 5000 - i, 0) for i in range(1, 6)]
    details.data[3] = RateLimited("SteamSpy is rate limiting us.")
    result = find_games(session, ["Tag"], 5)
    assert result.added == 2 and "rate limiting" in result.aborted
    assert enrich.log == []  # it did not carry on to the store step


def test_rate_limiting_while_fetching_store_details_stops_but_keeps_the_games(session, lists, details, enrich):
    lists.data["Tag"] = [listing(i, 5000 - i, 0) for i in range(1, 5)]
    enrich.fail[3] = RateLimited("Steam is rate limiting us.")
    result = find_games(session, ["Tag"], 4)
    assert result.added == 4 and result.enriched == 2 and "rate limiting" in result.aborted
    assert session.get(Game, 4).details_fetched_at is None  # left for the next run


def test_one_failing_game_is_skipped_but_three_in_a_row_stop_the_search(session, lists, details, enrich):
    lists.data["Tag"] = [listing(i, 5000 - i, 0) for i in range(1, 12)]
    details.data[2] = requests.ConnectionError("blip")
    result = find_games(session, ["Tag"], 3)
    assert result.failed == 1 and result.added == 3 and result.aborted is None

    for i in (5, 6, 7):
        details.data[i] = requests.ConnectionError("down")
    result = find_games(session, ["Tag"], 5)
    assert result.aborted and "network" in result.aborted.lower()


def test_store_detail_failures_are_counted_and_retried_next_time(session, lists, details, enrich):
    lists.data["Tag"] = [listing(1), listing(2)]
    enrich.fail[1] = requests.ConnectionError("blip")
    result = find_games(session, ["Tag"], 2)
    assert (result.added, result.enriched, result.failed) == (2, 1, 1)
    enrich.fail.clear()
    again = find_games(session, ["Tag"], 0)  # nothing new wanted: just finish what is pending
    assert again.enriched == 1


# ---- the background job ----


@pytest.fixture
def inline_job():
    """A job whose work runs right away, in the calling thread."""
    return DiscoverJob(spawn=lambda fn: fn())


def test_a_finished_search_reports_what_it_added(inline_job, lists, session):
    lists.data["Tag"] = [listing(i) for i in range(1, 4)]
    assert inline_job.start(["Tag"], 2, 300) is True
    snap = inline_job.snapshot()
    assert snap.state == "done" and "Added 2 new games" in snap.message and not snap.running


def test_only_one_search_runs_at_a_time():
    held = []
    job = DiscoverJob(spawn=held.append)  # never runs the work
    assert job.start(["A"], 5, 300) is True and job.snapshot().running
    assert job.start(["B"], 5, 300) is False
    job.fail("not now")  # cannot replace a running search's state
    assert job.snapshot().running


def test_a_stopped_search_says_so(lists, session):
    lists.data["Tag"] = [listing(i) for i in range(1, 6)]
    held = []
    job = DiscoverJob(spawn=held.append)
    job.start(["Tag"], 5, 300)
    job.cancel()
    held[0]()
    assert job.snapshot().state == "stopped" and job.snapshot().message.startswith("Stopped.")


def test_a_crash_never_leaves_the_page_stuck_on_running(monkeypatch):
    monkeypatch.setattr("shelflife.discover_job.find_games", lambda *a, **k: 1 / 0)
    job = DiscoverJob(spawn=lambda fn: fn())
    job.start(["Tag"], 5, 300)
    snap = job.snapshot()
    assert snap.failed and not snap.running and "division" in snap.message


def test_a_new_search_can_start_after_one_finished(inline_job, lists, session):
    lists.data["Tag"] = [listing(1)]
    inline_job.start(["Tag"], 1, 300)
    assert inline_job.start(["Tag"], 1, 300) is True


@pytest.mark.parametrize(
    "result, state, fragment",
    [
        (FindResult(added=3, enriched=3), "done", "Added 3 new games"),
        (FindResult(added=1, enriched=1), "done", "Added 1 new game."),
        (FindResult(), "done", "No new games found"),
        (FindResult(added=2, enriched=2, checked=9, skipped=7), "done", "looked at 9 and set aside 7"),
        (FindResult(added=2, enriched=2, failed=1), "done", "1 could not be fetched"),
        (FindResult(empty_tags=["Typo"]), "done", "no games for “Typo”"),
        (FindResult(added=2, enriched=1, aborted="rate limited."), "error", "Stopped early: rate limited."),
        (FindResult(added=2, enriched=1, stopped=True), "stopped", "Stopped."),
    ],
)
def test_the_outcome_message(result, state, fragment):
    snap = summarize(result, ["Tag"])
    assert snap.state == state and fragment in snap.message


# ---- the button on the Explore page ----


@pytest.fixture
def client(monkeypatch):
    held = []
    job = web_app.discover_job
    monkeypatch.setattr(job, "_snapshot", JobSnapshot())
    monkeypatch.setattr(job, "_spawn", held.append)  # the search never really runs here
    monkeypatch.setattr(job, "_stop", threading.Event())
    with TestClient(web_app.app) as c:
        c.held = held
        yield c


def start_args(monkeypatch):
    calls = []

    def start(tags, count, min_reviews):
        calls.append((tags, count, min_reviews))
        return True

    monkeypatch.setattr(web_app.discover_job, "start", start)
    return calls


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def rate_the_scenario(session):
    build_scenario(session)
    for app_id, rating in {1: 5.0, 2: 1.0}.items():
        diary.save_rating(session, app_id, rating)


def test_explore_offers_the_search_with_your_taste_tags_as_the_default(client, session):
    rate_the_scenario(session)
    html = client.get("/explore").text
    assert 'action="/explore/find"' in html and "Find games" in html
    assert "empty: your taste, Stealth" in html
    assert '<option value="Stealth">' in html


def test_pressing_the_button_starts_a_search_for_the_typed_tags_with_their_proper_spelling(client, session, monkeypatch):
    add_game(session, 1, "A", tags={"Roguelike": 10, "Co-op": 5})
    calls = start_args(monkeypatch)
    response = post(client, "/explore/find", tags="roguelike, CO-OP, Brand New Tag", count="50", min_reviews="1000")
    assert response.status_code == 303 and response.headers["location"] == "/explore#find"
    assert calls == [(["Roguelike", "Co-op", "Brand New Tag"], 50, 1000)]


def test_the_button_falls_back_to_your_taste_tags_when_nothing_is_typed(client, session, monkeypatch):
    rate_the_scenario(session)
    calls = start_args(monkeypatch)
    post(client, "/explore/find", tags="")
    tags, count, min_reviews = calls[0]
    assert tags[0] == "Stealth" and count == 25 and min_reviews == discovery.MIN_REVIEWS


@pytest.mark.parametrize("count, min_reviews", [("7", "5"), ("abc", "x"), ("-3", ""), ("1000000", "99999")])
def test_odd_amounts_fall_back_to_the_defaults(client, session, monkeypatch, count, min_reviews):
    add_game(session, 1, "A", tags={"Roguelike": 10})
    calls = start_args(monkeypatch)
    post(client, "/explore/find", tags="Roguelike", count=count, min_reviews=min_reviews)
    assert calls[0][1:] == (25, 300)


def test_no_more_than_six_tags_are_searched(client, session, monkeypatch):
    calls = start_args(monkeypatch)
    post(client, "/explore/find", tags=",".join(f"Tag{i}" for i in range(12)))
    assert len(calls[0][0]) == 6


def test_with_no_taste_and_no_tags_the_page_explains_what_to_do(client, session):
    post(client, "/explore/find", tags="")
    html = client.get("/explore").text
    assert "Rate a few games first" in html and not client.held


def test_while_searching_the_page_shows_progress_refreshes_and_can_stop(client, session):
    add_game(session, 1, "A", tags={"Roguelike": 10})
    post(client, "/explore/find", tags="Roguelike")
    assert len(client.held) == 1  # the search was handed to the background
    html = client.get("/explore").text
    assert '<meta http-equiv="refresh"' in html and "<progress" in html
    assert 'action="/explore/find/stop"' in html and 'action="/explore/find"' not in html
    post(client, "/explore/find/stop")
    assert web_app.discover_job._stop.is_set()


def test_a_finished_search_shows_its_message_and_stops_refreshing(client, session, lists):
    add_game(session, 1, "A", tags={"Roguelike": 10})
    lists.data["Roguelike"] = [listing(i) for i in range(50, 53)]
    post(client, "/explore/find", tags="Roguelike", count="10")
    client.held[0]()  # the background work, run now
    html = client.get("/explore").text
    assert "Added 3 new games" in html and "http-equiv" not in html


def test_a_search_started_from_another_website_is_refused(client, session, monkeypatch):
    calls = start_args(monkeypatch)
    response = client.post(
        "/explore/find", data={"tags": "Roguelike"}, headers={"Origin": "https://evil.example"}, follow_redirects=False
    )
    assert response.status_code == 403 and calls == []


def test_a_second_press_while_searching_does_not_start_another(client, session):
    add_game(session, 1, "A", tags={"Roguelike": 10})
    post(client, "/explore/find", tags="Roguelike")
    post(client, "/explore/find", tags="Roguelike")
    assert len(client.held) == 1


def test_set_aside_games_never_show_up_in_explore_or_the_library(client, session, lists, details):
    rate_the_scenario(session)
    lists.data["Stealth"] = [listing(500, 9000, 0, "Pizza Palace"), listing(501, 8000, 0, "Real Stealth Game")]
    details.data[500] = about("Pizza")
    post(client, "/explore/find", tags="Stealth", count="10")
    client.held[0]()
    assert "Pizza Palace" not in client.get("/explore").text
    assert "Pizza Palace" not in client.get("/library").text
    assert "Pizza Palace" not in client.get("/").text
