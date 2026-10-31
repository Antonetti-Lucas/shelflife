"""Searching for new games without tags: the best reviewed of SteamSpy's list of popular games."""

import threading

import pytest
import requests
from fastapi.testclient import TestClient

from shelflife import discovery, store
from shelflife.discover_job import DiscoverJob, JobSnapshot, summarize
from shelflife.discovery import POPULAR_PAGES, FindResult, find_games
from shelflife.models import Game
from shelflife.store import SpyListing
from shelflife.throttle import RateLimited
from shelflife.web import app as web_app
from tests.conftest import FakeResponse
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}
ALL_PAGES_INTERVAL = store.steamspy_all_throttle.min_interval  # before any test setup zeroes it


def listing(app_id, positive=900, negative=100, name=None):
    return SpyListing(app_id, name or f"Game {app_id}", positive, negative)


# ---- fetching a page of SteamSpy's full list ----


@pytest.fixture(autouse=True)
def fresh_pages(monkeypatch):
    monkeypatch.setattr(store, "_pages", {})


@pytest.fixture
def http(monkeypatch):
    """Fake the web: each call is recorded; the handler gives the response."""
    calls = []
    handler = {"respond": lambda params: FakeResponse(200, {})}

    def get(url, params=None, **kw):
        calls.append(params)
        return handler["respond"](params)

    monkeypatch.setattr(store.requests, "get", get)
    get.calls, get.handler = calls, handler
    return get


def test_a_page_of_popular_games_is_parsed(http):
    body = {"1": {"appid": 1, "name": "A", "positive": 90, "negative": 10}, "2": {"appid": 2, "name": "B"}}
    http.handler["respond"] = lambda params: FakeResponse(200, body)
    games = store.fetch_spy_page(0)
    assert [(g.app_id, g.name, g.positive, g.negative) for g in games] == [(1, "A", 90, 10), (2, "B", 0, 0)]
    assert http.calls == [{"request": "all", "page": 0}]


def test_a_page_is_remembered_so_asking_again_makes_no_request(http):
    http.handler["respond"] = lambda params: FakeResponse(200, {"1": {"appid": 1, "name": "A"}})
    store.fetch_spy_page(0)
    store.fetch_spy_page(0)
    assert len(http.calls) == 1
    store.fetch_spy_page(1)
    assert len(http.calls) == 2  # a different page is a new request


def test_a_remembered_page_expires(http):
    http.handler["respond"] = lambda params: FakeResponse(200, {"1": {"appid": 1, "name": "A"}})
    store.fetch_spy_page(0)
    stamped, listings = store._pages[0]
    store._pages[0] = (stamped - store.PAGE_CACHE_SECONDS - 1, listings)  # it is now a few hours old
    store.fetch_spy_page(0)
    assert len(http.calls) == 2


def test_a_page_request_waits_as_steamspy_asks_but_a_remembered_page_does_not(http, monkeypatch):
    waits = []
    monkeypatch.setattr(store.steamspy_all_throttle, "wait", lambda: waits.append(1))
    http.handler["respond"] = lambda params: FakeResponse(200, {"1": {"appid": 1, "name": "A"}})
    store.fetch_spy_page(0)
    store.fetch_spy_page(0)  # remembered
    store.fetch_spy_page(1)
    assert len(waits) == 2  # one wait per real request


def test_the_wait_between_page_requests_is_a_minute():
    # Read at import time: the test setup sets every throttle to zero so tests never sleep.
    assert ALL_PAGES_INTERVAL >= 60


def test_a_rate_limited_page_raises_and_is_not_remembered(http):
    http.handler["respond"] = lambda params: FakeResponse(429, {})
    with pytest.raises(RateLimited):
        store.fetch_spy_page(0)
    assert store._pages == {}


def test_a_failed_page_raises_and_is_not_remembered(http):
    http.handler["respond"] = lambda params: FakeResponse(500, {})
    with pytest.raises(requests.HTTPError):
        store.fetch_spy_page(0)
    assert store._pages == {}


# ---- the search ----


@pytest.fixture
def pages(monkeypatch):
    """Fake SteamSpy pages: page number -> listings (or an exception to raise)."""
    data, asked = {}, []

    def fetch(page, timeout=120):
        asked.append(page)
        item = data.get(page, [])
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(store, "fetch_spy_page", fetch)
    fetch.data, fetch.asked = data, asked
    return fetch


@pytest.fixture(autouse=True)
def no_tag_lookups(monkeypatch):
    """A search without tags must never ask for a game's tags: that would be a tag search."""

    def refuse(*args, **kwargs):
        raise AssertionError("a search without tags asked SteamSpy about tags")

    monkeypatch.setattr(store, "fetch_spy_tag_games", refuse)
    monkeypatch.setattr(store, "fetch_spy_tag_list", refuse)
    monkeypatch.setattr(store, "fetch_spy_details", refuse)


@pytest.fixture
def enrich(monkeypatch):
    log, kinds = [], {}

    def fake(game, spy=None):
        log.append(game.steam_app_id)
        game.type = kinds.get(game.steam_app_id, "game")
        game.tags = game.tags or {"Stealth": 100}
        game.details_fetched_at = discovery._now()

    monkeypatch.setattr(discovery, "enrich_one", fake)
    fake.log, fake.kinds = log, kinds
    return fake


def test_the_best_reviewed_popular_games_you_do_not_have_are_added(session, pages, enrich):
    pages.data[0] = [listing(i, 1000 - i, i) for i in range(1, 11)]  # 1 is the best reviewed
    result = find_games(session, [], 3, popular=True)
    assert (result.added, result.enriched) == (3, 3)
    assert sorted(enrich.log) == [1, 2, 3]
    assert pages.asked == [0]


def test_games_you_have_and_games_with_few_reviews_are_left_out(session, pages, enrich):
    add_game(session, 1, "Owned")
    pooled = add_game(session, 2, "In the pool", owned=False)
    pooled.details_fetched_at = discovery._now()  # already fully fetched: nothing left to finish for it
    session.commit()
    pages.data[0] = [listing(1, 9000, 0), listing(2, 8000, 0), listing(3, 100, 100), listing(4, 700, 100)]
    result = find_games(session, [], 10, popular=True)
    assert sorted(enrich.log) == [4] and result.added == 1  # 3 has only 200 reviews
    assert session.get(Game, 3) is None  # too few reviews: never even stored
    assert session.get(Game, 4) is not None and session.query(Game).count() == 3  # the two it had, plus 4


def test_it_goes_to_the_next_page_only_when_it_needs_more_games(session, pages, enrich):
    pages.data[0] = [listing(i, 5000 - i, 0) for i in range(1, 4)]
    pages.data[1] = [listing(i, 4000 - i, 0) for i in range(11, 16)]
    find_games(session, [], 2, popular=True)
    assert pages.asked == [0]  # page 0 had enough
    find_games(session, [], 20, popular=True)
    assert pages.asked == [0, 0, 1, 2]  # now it read on, until the pages ran out of ideas


def test_games_with_too_few_reviews_do_not_count_towards_having_enough(session, pages, enrich):
    # Page 0 lists four games but only one has enough reviews: wanting three, it must read page 1.
    pages.data[0] = [listing(1, 900, 100)] + [listing(i, 60, 40) for i in (2, 3, 4)]
    pages.data[1] = [listing(11, 800, 100), listing(12, 700, 100)]
    result = find_games(session, [], 3, popular=True)
    assert pages.asked == [0, 1] and result.added == 3 and sorted(enrich.log) == [1, 11, 12]


def test_it_never_reads_more_than_the_page_limit(session, pages, enrich):
    for page in range(10):
        pages.data[page] = []
    find_games(session, [], 5, popular=True)
    assert pages.asked == list(range(POPULAR_PAGES))


def test_the_best_of_all_the_pages_it_needed_to_read_win(session, pages, enrich):
    pages.data[0] = [listing(1, 600, 400)]  # 60% positive: one game, so page 0 alone is not enough for two
    pages.data[1] = [listing(2, 9900, 100), listing(3, 5000, 100)]
    result = find_games(session, [], 2, popular=True)
    assert result.added == 2 and sorted(enrich.log) == [2, 3]  # the better reviewed of everything read


def test_it_stops_reading_once_a_page_has_enough_even_if_a_later_page_is_better(session, pages, enrich):
    pages.data[0] = [listing(5, 600, 400)]
    pages.data[1] = [listing(6, 9900, 100)]
    find_games(session, [], 1, popular=True)
    assert enrich.log == [5] and pages.asked == [0]  # a page a minute: it does not read more than it needs


def test_no_tag_is_needed_and_no_game_is_checked_against_one(session, pages, enrich):
    pages.data[0] = [listing(1)]
    assert find_games(session, [], 5, popular=True).added == 1  # the autouse guard fails on any tag lookup


def test_things_that_are_not_games_are_counted_and_stay_hidden(session, pages, enrich):
    pages.data[0] = [listing(1, 5000, 0), listing(2, 4000, 0)]
    enrich.kinds[2] = "dlc"
    result = find_games(session, [], 5, popular=True)
    assert (result.added, result.enriched, result.not_games) == (2, 2, 1)


def test_progress_names_the_page_and_says_when_it_waits(session, pages, enrich):
    pages.data[0] = [listing(1)]
    pages.data[1] = [listing(2)]
    steps = []
    find_games(session, [], 5, popular=True, on_progress=lambda *a: steps.append(a))
    texts = [s[3] for s in steps if s[0] == "lists"]
    assert "page 1" in texts[0] and "waits" not in texts[0]
    assert "page 2" in texts[1] and "waits" in texts[1]
    assert steps[-1][0] == "done"


def test_a_rate_limited_page_stops_the_search_and_says_why(session, pages, enrich):
    pages.data[0] = RateLimited("SteamSpy is rate limiting us.")
    result = find_games(session, [], 5, popular=True)
    assert result.added == 0 and "rate limiting" in result.aborted


def test_a_network_error_is_reported_not_raised(session, pages, enrich):
    pages.data[0] = requests.ConnectionError("down")
    result = find_games(session, [], 5, popular=True)
    assert result.added == 0 and "Could not reach SteamSpy" in result.aborted


def test_stopping_before_reading_a_page_adds_nothing(session, pages, enrich):
    pages.data[0] = [listing(1)]
    result = find_games(session, [], 5, popular=True, should_stop=lambda: True)
    assert result.stopped and result.added == 0 and pages.asked == []


def test_a_later_failure_in_the_store_step_keeps_the_added_games(session, pages, enrich, monkeypatch):
    pages.data[0] = [listing(i, 5000 - i, 0) for i in range(1, 4)]

    def flaky(game, spy=None):
        if game.steam_app_id == 2:
            raise RateLimited("Steam is rate limiting us.")
        game.type, game.details_fetched_at = "game", discovery._now()

    monkeypatch.setattr(discovery, "enrich_one", flaky)
    result = find_games(session, [], 3, popular=True)
    assert result.added == 3 and "rate limiting" in result.aborted
    assert session.get(Game, 3).details_fetched_at is None  # left for the next run


# ---- the job and its messages ----


def test_a_popular_search_is_flagged_in_the_state(pages, enrich, session):
    pages.data[0] = [listing(1)]
    job = DiscoverJob(spawn=lambda fn: fn())
    job.start([], 5, 300, popular=True)
    snap = job.snapshot()
    assert snap.state == "done" and snap.popular and "Added 1 new game" in snap.message


def test_a_running_popular_search_says_so():
    held = []
    job = DiscoverJob(spawn=held.append)
    job.start([], 5, 300, popular=True)
    assert job.snapshot().running and job.snapshot().popular


def test_the_message_mentions_what_was_not_a_game():
    snap = summarize(FindResult(added=3, enriched=3, not_games=2), [], popular=True)
    assert "2 turned out not to be games" in snap.message and snap.popular


# ---- the button ----


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


def start_calls(monkeypatch):
    calls = []

    def start(tags, count, min_reviews, like=None, popular=False):
        calls.append((tags, count, min_reviews, like, popular))
        return True

    monkeypatch.setattr(web_app.discover_job, "start", start)
    return calls


def test_explore_offers_a_choice_between_tags_and_popular_games(client, session):
    html = client.get("/explore").text
    assert 'name="source"' in html and 'value="tags" selected' in html and 'value="popular"' in html
    assert "Well-reviewed games, no tags" in html


def test_choosing_popular_games_starts_a_search_without_tags(client, session, monkeypatch):
    calls = start_calls(monkeypatch)
    response = post(client, "/explore/find", source="popular", tags="Roguelike, Co-op", count="50", min_reviews="1000")
    assert response.status_code == 303 and response.headers["location"] == "/explore#find"
    assert calls == [([], 50, 1000, None, True)]  # the typed tags are ignored


def test_it_works_even_when_there_is_no_taste_to_search_for(client, session, monkeypatch):
    calls = start_calls(monkeypatch)  # empty library and diary: the tag search would have nothing to use
    post(client, "/explore/find", source="popular")
    assert calls and calls[0][4] is True
    assert "Rate a few games first" not in client.get("/explore").text


@pytest.mark.parametrize("source", ["tags", "", "banana"])
def test_anything_else_is_the_tag_search(client, session, monkeypatch, source):
    add_game(session, 1, "A", tags={"Roguelike": 10})
    calls = start_calls(monkeypatch)
    post(client, "/explore/find", source=source, tags="Roguelike")
    assert calls[0][0] == ["Roguelike"] and calls[0][4] is False


def test_the_page_describes_the_steps_of_a_popular_search(client, session):
    post(client, "/explore/find", source="popular")
    html = client.get("/explore").text
    assert "Step 1 of 2: reading SteamSpy's popular games" in html and "<progress" in html


def test_a_finished_popular_search_shows_what_it_found(client, session, pages, enrich):
    pages.data[0] = [listing(i, 5000 - i, 0) for i in range(1, 4)]
    post(client, "/explore/find", source="popular", count="10")
    client.held[0]()
    html = client.get("/explore").text
    assert "Added 3 new games" in html and "Game 1" in html


# ---- games an earlier tag search set aside ----


def test_a_game_a_tag_search_set_aside_is_still_a_popular_candidate(session, pages, enrich):
    # Regression, found live: such a game keeps its row, and adding it again crashed with a
    # duplicate-key error. Off-topic for one tag says nothing against it as a popular game.
    add_game(session, 7, "Set aside", tags={"Pizza": 100}, owned=False)
    row = session.get(Game, 7)
    row.type, row.details_fetched_at = discovery.OFF_TOPIC, discovery._now()
    session.commit()
    pages.data[0] = [listing(7, 9000, 100), listing(8, 800, 100)]
    result = find_games(session, [], 5, popular=True)
    assert result.added == 2 and result.aborted is None
    assert sorted(enrich.log) == [7, 8]
    assert session.get(Game, 7).type == "game" and session.query(Game).filter(Game.steam_app_id == 7).count() == 1


def test_a_crash_is_reported_as_one_short_line():
    def boom(*args, **kwargs):
        raise RuntimeError("first line\nSQL: INSERT INTO games ...\nparameters: [...]")

    import shelflife.discover_job as dj

    original = dj.find_games
    dj.find_games = boom
    try:
        job = DiscoverJob(spawn=lambda fn: fn())
        job.start([], 5, 300, popular=True)
    finally:
        dj.find_games = original
    message = job.snapshot().message
    assert message == "Stopped early: first line" and "SQL" not in message
