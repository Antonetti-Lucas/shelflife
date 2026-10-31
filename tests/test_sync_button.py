"""The Sync button: the four steps in the background, stopping, failing, and the page."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from shelflife import sync, sync_job
from shelflife.achievements import AchievementStats
from shelflife.jobs import JobSnapshot
from shelflife.models import Game, LibraryEntry, WishlistItem
from shelflife.steam import OwnedGame, SteamError
from shelflife.sync_job import STEPS, SyncJob, SyncResult, run_sync, summarize_sync, what_is_due
from shelflife.throttle import RateLimited
from shelflife.wishlist import WishlistEntry
from shelflife.web import app as web_app
from tests.factories import add_game

SAME_SITE = {"Origin": "http://testserver"}


def owned(app_id, minutes=0, recent=0, name=None):
    return OwnedGame(app_id, name or f"Game {app_id}", minutes, recent, 0)


# ---- fakes: no network ----


@pytest.fixture
def steam(monkeypatch):
    """A Steam that owns whatever `steam.library` says, and records what was asked of it."""
    state = SimpleNamespace(library=[], wishlist=[], achievements=[], details=[], reviews=[], fail={}, order=[])

    def fetch_owned(api_key, steam_id):
        state.order.append("library")
        if "library" in state.fail:
            raise state.fail["library"]
        return list(state.library)

    def fetch_wishlist(steam_id):
        state.order.append("wishlist")
        if "wishlist" in state.fail:
            raise state.fail["wishlist"]
        return list(state.wishlist)

    def fetch_achievements(api_key, steam_id, app_id):
        state.order.append("achievements")
        state.achievements.append(app_id)
        if "achievements" in state.fail:
            raise state.fail["achievements"]
        return AchievementStats(10, 2, 5.0, datetime(2020, 1, 1), datetime(2021, 1, 1), (100, 200))

    def enrich_one(game, spy=None):
        state.order.append("details")
        state.details.append(game.steam_app_id)
        if "details" in state.fail:
            raise state.fail["details"]
        game.type, game.tags, game.details_fetched_at = "game", {"Stealth": 10}, datetime.now()  # fetched just now

    def fetch_review_counts(app_id):
        state.order.append("reviews")
        state.reviews.append(app_id)
        if "reviews" in state.fail:
            raise state.fail["reviews"]
        return SimpleNamespace(positive=900, negative=100)

    monkeypatch.setattr(sync_job, "fetch_owned_games", fetch_owned)
    monkeypatch.setattr(sync_job, "fetch_wishlist", fetch_wishlist)
    monkeypatch.setattr(sync, "fetch_achievements", fetch_achievements)
    monkeypatch.setattr(sync, "enrich_one", enrich_one)
    monkeypatch.setattr(sync, "fetch_review_counts", fetch_review_counts)
    return state


# ---- the steps ----


def test_a_sync_runs_all_the_steps_in_order_and_saves_everything(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=0)]
    steps = []
    result = run_sync(session, "key", "id", on_progress=lambda step, *a: steps.append(step))
    assert (result.games, result.new_games) == (2, 2)
    assert (result.achievements, result.details, result.reviews) == (1, 2, 2)  # every game gets review counts
    assert result.aborted is None and not result.stopped
    assert sorted(dict.fromkeys(steps)) == [1, 2, 3, 4, 5] and steps == sorted(steps)  # never goes back a step
    assert session.get(LibraryEntry, 1).playtime_total_min == 600


def test_the_steps_happen_in_this_order_and_each_finishes_before_the_next_begins(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=600), owned(3, minutes=0)]
    run_sync(session, "key", "id")
    firsts = list(dict.fromkeys(steam.order))
    assert firsts == ["library", "wishlist", "achievements", "details", "reviews"]
    runs = [key for i, key in enumerate(steam.order) if i == 0 or key != steam.order[i - 1]]
    assert runs == firsts  # no step comes back after the next one has started


def test_each_step_only_does_what_is_due(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=0)]
    run_sync(session, "key", "id")
    assert steam.achievements == [1]  # only the game you played
    assert sorted(steam.details) == [1, 2]  # both are new
    assert sorted(steam.reviews) == [1, 2]  # played games too, not only the backlog


def test_a_second_sync_has_nothing_new_to_fetch(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=0)]
    run_sync(session, "key", "id")
    steam.achievements.clear(); steam.details.clear(); steam.reviews.clear()  # noqa: E702
    second = run_sync(session, "key", "id")
    assert (second.games, second.new_games) == (2, 0)
    assert steam.details == [] and steam.reviews == [] and steam.achievements == []
    assert (second.achievements, second.details, second.reviews) == (0, 0, 0)


def test_a_new_game_on_the_next_sync_is_counted_and_gets_its_details(session, steam):
    steam.library = [owned(1, minutes=600)]
    run_sync(session, "key", "id")
    steam.details.clear()
    steam.library = [owned(1, minutes=700), owned(3, minutes=0)]
    second = run_sync(session, "key", "id")
    assert second.new_games == 1 and steam.details == [3]
    assert session.get(LibraryEntry, 1).playtime_total_min == 700  # playtime refreshed


def test_games_already_known_are_not_counted_as_new(session, steam):
    add_game(session, 1, "Known", minutes=0)  # e.g. from an earlier sync or a family import
    steam.library = [owned(1, minutes=10), owned(2)]
    assert run_sync(session, "key", "id").new_games == 1


def test_a_full_check_reads_everything_again_but_a_normal_sync_does_not(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=0)]
    run_sync(session, "key", "id")
    for calls in (steam.achievements, steam.details, steam.reviews):
        calls.clear()
    normal = run_sync(session, "key", "id")
    assert (normal.achievements, normal.details, normal.reviews) == (0, 0, 0)
    full = run_sync(session, "key", "id", full=True)
    assert full.full and (full.achievements, full.details, full.reviews) == (1, 2, 2)
    assert "Checked everything again" in summarize_sync(full).message


def test_a_normal_sync_retries_games_that_came_back_empty_and_refreshes_old_ones(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=600), owned(3, minutes=600)]
    run_sync(session, "key", "id")
    steam.details.clear()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    one, two, three = (session.get(Game, i) for i in (1, 2, 3))
    one.type, one.details_fetched_at = "unavailable", now - timedelta(days=10)  # empty ten days ago
    two.details_fetched_at = now - timedelta(days=120)  # old
    three.details_fetched_at = now - timedelta(days=5)  # recent: left alone
    session.commit()
    run_sync(session, "key", "id")
    assert sorted(steam.details) == [1, 2]


def test_a_game_played_since_the_last_sync_gets_its_achievements_read_again(session, steam):
    steam.library = [owned(1, minutes=600)]
    run_sync(session, "key", "id")
    steam.achievements.clear()
    now = datetime.now(timezone.utc)
    session.get(LibraryEntry, 1).achievements_fetched_at = (now - timedelta(days=20)).replace(tzinfo=None)
    session.commit()
    # Steam now says it was last played 18 days ago: after the last read, but not in the last two weeks.
    steam.library = [OwnedGame(1, "Game 1", 700, 0, int((now - timedelta(days=18)).timestamp()))]
    assert run_sync(session, "key", "id").achievements == 1 and steam.achievements == [1]


# ---- the wishlist ----


def wanted(app_id, priority=0):
    return WishlistEntry(app_id, priority, datetime(2024, 5, 1))


def test_a_sync_reads_the_wishlist_and_says_so(session, steam):
    steam.library = [owned(1, minutes=600)]
    steam.wishlist = [wanted(50, 3), wanted(51)]
    result = run_sync(session, "key", "id")
    assert (result.wishlist, result.wishlist_added) == (2, 2) and result.wishlist_note is None
    assert session.get(WishlistItem, 50).priority == 3
    assert "2 on your wishlist (2 new)" in summarize_sync(result).message


def test_wishlist_games_get_store_details_and_review_counts_but_no_achievements(session, steam):
    steam.library = [owned(1, minutes=600)]
    steam.wishlist = [wanted(50)]
    run_sync(session, "key", "id")
    assert 50 in steam.details and 50 in steam.reviews
    assert steam.achievements == [1]  # you cannot have achievements in a game you do not own


def test_the_second_sync_adds_nothing_new_and_drops_what_you_bought(session, steam):
    steam.library = [owned(1, minutes=600)]
    steam.wishlist = [wanted(50), wanted(51)]
    run_sync(session, "key", "id")
    steam.wishlist = [wanted(51)]  # you bought 50
    second = run_sync(session, "key", "id")
    assert (second.wishlist, second.wishlist_added) == (1, 0)
    assert session.get(WishlistItem, 50) is None and session.get(WishlistItem, 51) is not None


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), RateLimited("slow down"), ValueError("not json")])
def test_a_wishlist_that_cannot_be_read_does_not_stop_the_rest_of_the_sync(session, steam, failure):
    steam.library = [owned(1, minutes=600)]
    steam.fail["wishlist"] = failure
    result = run_sync(session, "key", "id")
    assert result.aborted is None and "could not be read" in result.wishlist_note
    assert (result.achievements, result.details, result.reviews) == (1, 1, 1)  # everything else still happened
    snap = summarize_sync(result)
    assert snap.state == "done" and "your wishlist: it could not be read" in snap.message


def test_an_empty_answer_keeps_the_earlier_wishlist_and_says_why(session, steam):
    steam.library = [owned(1, minutes=600)]
    steam.wishlist = [wanted(50)]
    run_sync(session, "key", "id")
    steam.wishlist = []  # private now, or Steam failed
    result = run_sync(session, "key", "id")
    assert result.wishlist == 1 and "earlier one was kept" in result.wishlist_note
    assert session.get(WishlistItem, 50) is not None


def test_stopping_right_after_the_wishlist_keeps_it_and_skips_the_rest(session, steam):
    steam.library = [owned(1, minutes=600)]
    steam.wishlist = [wanted(50)]
    result = run_sync(session, "key", "id", should_stop=lambda: "wishlist" in steam.order)
    assert result.stopped and result.wishlist == 1 and steam.achievements == []
    assert session.get(WishlistItem, 50) is not None


def test_the_sync_page_counts_the_wishlist(client, session):
    from shelflife.wishlist import import_wishlist

    add_game(session, 1, minutes=60)
    import_wishlist(session, [wanted(50), wanted(51), wanted(52)])
    assert ", 3 on your wishlist." in client.get("/sync").text
    assert what_is_due(session)["wishlist"] == 3


# ---- stopping ----


def test_stopping_right_after_the_library_keeps_the_library_and_does_the_rest_later(session, steam):
    steam.library = [owned(1, minutes=600)]
    result = run_sync(session, "key", "id", should_stop=lambda: True)
    assert result.stopped and result.games == 1 and steam.achievements == []
    assert session.get(LibraryEntry, 1) is not None
    carry_on = run_sync(session, "key", "id")  # the next sync carries on
    assert carry_on.achievements == 1 and carry_on.details == 1


def test_stopping_in_the_middle_of_a_slow_step_keeps_what_was_done(session, steam):
    steam.library = [owned(i, minutes=600) for i in (1, 2, 3, 4)]
    result = run_sync(session, "key", "id", should_stop=lambda: len(steam.details) >= 2)
    assert result.stopped and result.details == 2 and result.reviews == 0
    assert session.query(Game).filter(Game.details_fetched_at.is_not(None)).count() == 2
    steam.details.clear()
    run_sync(session, "key", "id")
    assert sorted(steam.details) == [3, 4]  # exactly the ones left


def test_stopping_during_the_achievements_step_keeps_what_was_done_and_skips_the_rest(session, steam):
    steam.library = [owned(i, minutes=600) for i in (1, 2, 3, 4)]
    result = run_sync(session, "key", "id", should_stop=lambda: len(steam.achievements) >= 2)
    assert result.stopped and result.achievements == 2 and result.details == 0 and steam.details == []


@pytest.mark.parametrize("function, args", [
    (sync.sync_achievements, ("key", "id")),
    (sync.enrich_games, ()),
    (sync.sync_backlog_reviews, (60,)),
])  # fmt: skip
def test_every_slow_step_can_be_stopped_before_it_starts(session, steam, function, args):
    add_game(session, 1, minutes=0)
    add_game(session, 2, minutes=600)
    assert function(session, *args, should_stop=lambda: True) == 0
    assert steam.achievements == [] and steam.details == [] and steam.reviews == []


def test_a_step_stopped_after_some_games_returns_how_many_it_did(session, steam):
    for i in (1, 2, 3):
        add_game(session, i, minutes=600)
    done = []
    count = sync.sync_achievements(
        session, "key", "id", on_progress=lambda *a: done.append(1), should_stop=lambda: len(done) >= 2
    )
    assert count == 2 and len(steam.achievements) == 2


# ---- failing ----


def test_a_steam_error_on_the_library_stops_the_sync_with_the_reason(session, steam):
    steam.fail["library"] = SteamError("Steam rejected the API key (check STEAM_API_KEY).")
    result = run_sync(session, "key", "id")
    assert "rejected the API key" in result.aborted and result.games == 0
    assert session.query(Game).count() == 0


def test_three_failures_in_a_row_during_achievements_stop_the_sync_and_keep_the_library(session, steam):
    steam.library = [owned(i, minutes=600) for i in (1, 2, 3)]
    steam.fail["achievements"] = SteamError("Steam says 403")
    result = run_sync(session, "key", "id")
    assert "3 failures in a row" in result.aborted and result.games == 3 and result.details == 0
    assert session.get(LibraryEntry, 1) is not None


def test_one_game_steam_cannot_answer_for_does_not_stop_the_sync(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=600)]
    answers = iter([SteamError("blip"), None])
    original = sync.fetch_achievements

    def sometimes(api_key, steam_id, app_id):
        outcome = next(answers)
        if outcome is not None:
            raise outcome
        return original(api_key, steam_id, app_id)

    sync.fetch_achievements = sometimes
    try:
        result = run_sync(session, "key", "id")
    finally:
        sync.fetch_achievements = original
    assert result.aborted is None and result.achievements == 1 and result.details == 2


def test_rate_limiting_while_fetching_details_keeps_the_earlier_steps(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=600)]
    steam.fail["details"] = RateLimited("Steam is rate limiting us.")
    result = run_sync(session, "key", "id")
    assert "rate limiting" in result.aborted and result.achievements == 2 and result.details == 0
    assert result.reviews == 0  # it did not carry on to the next step


def test_a_network_error_is_reported_not_raised(session, steam):
    steam.library = [owned(1, minutes=600)]
    steam.fail["details"] = requests.ConnectionError("down")
    result = run_sync(session, "key", "id")
    assert result.aborted and result.aborted.startswith("Network error") and result.achievements == 1


# ---- what the page says ----


@pytest.mark.parametrize(
    "result, state, fragments",
    [
        (SyncResult(games=300, new_games=4, achievements=4, details=4, reviews=2), "done", ["300 owned games (4 new)", "achievements for 4 games", "store details for 4 games", "review counts for 2 games"]),
        (SyncResult(games=300), "done", ["300 owned games."]),
        (SyncResult(games=300, details=2, stopped=True), "stopped", ["Stopped.", "Sync again to carry on"]),
        (SyncResult(games=300, aborted="Steam is rate limiting us."), "error", ["Stopped early: Steam is rate limiting us.", "Progress is saved"]),
    ],
)  # fmt: skip
def test_the_outcome_message(result, state, fragments):
    snap = summarize_sync(result)
    assert snap.state == state
    for fragment in fragments:
        assert fragment in snap.message


def test_what_is_due_counts_what_the_next_sync_would_do(session):
    add_game(session, 1, minutes=600)  # played, nothing fetched yet
    add_game(session, 2, minutes=0)
    add_game(session, 3, minutes=600, achievements=(1, 5), unlock_times=[100])
    fresh = datetime.now()
    game3 = session.get(Game, 3)
    game3.details_fetched_at, game3.reviews_fetched_at = fresh, fresh
    session.get(LibraryEntry, 3).achievements_fetched_at = fresh
    session.commit()
    add_game(session, 4, owned=False)  # not yours: never due
    due = what_is_due(session)
    assert (due["library"], due["details"], due["achievements"], due["reviews"]) == (3, 2, 1, 2)
    assert (due["owned"], due["family"]) == (3, 0)
    assert due["last_synced"] is not None and due["minutes"] >= 1 and due["full_minutes"] >= due["minutes"]


def test_nothing_is_due_in_an_empty_database(session):
    due = what_is_due(session)
    assert (due["library"], due["details"], due["achievements"], due["reviews"], due["last_synced"]) == (0, 0, 0, 0, None)
    assert (due["minutes"], due["full_minutes"]) == (0, 0)


# ---- the background task ----


def test_a_background_sync_ends_with_its_message_and_never_holds_your_key(session, steam):
    steam.library = [owned(1, minutes=600)]
    job = SyncJob(spawn=lambda fn: fn())
    assert job.start("SECRET-KEY-123", "7656119000000000") is True
    snap = job.snapshot()
    assert snap.state == "done" and "1 owned game" in snap.message
    assert "SECRET-KEY-123" not in repr(snap) and "7656119000000000" not in repr(snap)


def test_a_running_sync_reports_its_step_and_refuses_a_second_start():
    held = []
    job = SyncJob(spawn=held.append)  # never runs the work
    assert job.start("k", "i") is True
    snap = job.snapshot()
    assert snap.running and (snap.step, snap.steps) == (1, len(STEPS))
    assert job.start("k", "i") is False


def test_progress_updates_the_step_and_the_text(session, steam):
    steam.library = [owned(1, minutes=600), owned(2, minutes=0)]  # a played game and a backlog game
    seen = []
    job = SyncJob(spawn=lambda fn: fn())
    original = job._set
    job._set = lambda **kw: (seen.append(kw), original(**kw))[1]
    job.start("k", "i")
    assert {kw["step"] for kw in seen} == {1, 2, 3, 4, 5}
    assert any("Store details for Game 1" in kw["text"] for kw in seen)


def test_stopping_a_background_sync_says_so_and_keeps_the_library(session, steam):
    steam.library = [owned(1, minutes=600)]
    held = []
    job = SyncJob(spawn=held.append)
    job.start("k", "i")
    job.cancel()
    held[0]()
    assert job.snapshot().state == "stopped"
    assert session.get(LibraryEntry, 1) is not None


def test_a_crash_never_leaves_the_page_stuck_on_running(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("first line\nSQL: more detail")

    monkeypatch.setattr(sync_job, "run_sync", boom)
    job = SyncJob(spawn=lambda fn: fn())
    job.start("k", "i")
    snap = job.snapshot()
    assert snap.failed and not snap.running and snap.message == "Stopped early: first line"


# ---- the page and its buttons ----


@pytest.fixture
def client(monkeypatch):
    held = []
    for name in ("sync_job", "discover_job"):
        job = getattr(web_app, name)
        monkeypatch.setattr(job, "_snapshot", JobSnapshot())
        monkeypatch.setattr(job, "_spawn", held.append)  # nothing really runs here
        monkeypatch.setattr(job, "_stop", threading.Event())
    with TestClient(web_app.app) as c:
        c.held = held
        yield c


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("STEAM_API_KEY", "SECRET-KEY-123")
    monkeypatch.setenv("STEAM_ID", "76561190000000000")


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


def test_the_sync_page_is_in_the_nav_and_loads_empty(client):
    html = client.get("/sync").text
    assert 'href="/sync"' in html and "Not synced yet" in html
    assert all(label in html for _, label in STEPS)


def test_without_a_key_the_page_explains_instead_of_offering_a_button(client):
    html = client.get("/sync").text
    assert "STEAM_API_KEY" in html and 'action="/sync/start"' not in html


def test_with_a_key_the_page_offers_the_button_and_never_shows_the_key(client, configured):
    html = client.get("/sync").text
    assert 'action="/sync/start"' in html and "Sync now" in html
    assert "SECRET-KEY-123" not in html and "76561190000000000" not in html


def test_pressing_sync_starts_it_with_your_key_and_comes_back_to_the_page(client, configured, monkeypatch):
    calls = []
    monkeypatch.setattr(web_app.sync_job, "start", lambda key, steam_id, full=False: calls.append((key, steam_id, full)) or True)
    response = post(client, "/sync/start")
    assert response.status_code == 303 and response.headers["location"] == "/sync"
    assert calls == [("SECRET-KEY-123", "76561190000000000", False)]


def test_checking_everything_again_starts_a_full_sync(client, configured, monkeypatch):
    calls = []
    monkeypatch.setattr(web_app.sync_job, "start", lambda key, steam_id, full=False: calls.append(full) or True)
    post(client, "/sync/start", full="1")
    post(client, "/sync/start", full="")
    post(client, "/sync/start", full="yes")  # only 1 means a full re-check
    assert calls == [True, False, False]


def test_pressing_sync_without_a_key_says_what_to_do_and_starts_nothing(client):
    post(client, "/sync/start")
    html = client.get("/sync").text
    assert "Add them and restart the app" in html and not client.held


def test_while_syncing_the_page_shows_the_step_and_a_stop_button(client, configured, session):
    post(client, "/sync/start")
    assert len(client.held) == 1  # handed to the background
    html = client.get("/sync").text
    assert '<meta http-equiv="refresh"' in html and "<progress" in html
    assert "Step 1 of 5: Reading your library" in html
    assert 'action="/sync/stop"' in html and 'action="/sync/start"' not in html
    post(client, "/sync/stop")
    assert web_app.sync_job._stop.is_set()


def test_a_second_press_while_syncing_does_not_start_another(client, configured):
    post(client, "/sync/start")
    post(client, "/sync/start")
    assert len(client.held) == 1


def test_a_finished_sync_shows_its_message_and_a_link_to_the_library(client, configured, session, steam):
    steam.library = [owned(1, minutes=600)]
    post(client, "/sync/start")
    client.held[0]()  # the background work, run now
    html = client.get("/sync").text
    assert "1 owned game" in html and 'href="/library"' in html and "http-equiv" not in html
    assert "Game 1" in client.get("/library").text


def test_a_failed_sync_shows_why(client, configured, session, steam):
    steam.fail["library"] = SteamError("Steam rejected the API key (check STEAM_API_KEY).")
    post(client, "/sync/start")
    client.held[0]()
    assert "rejected the API key" in client.get("/sync").text


def test_the_page_says_what_is_due_and_roughly_how_long(client, session):
    for i in range(1, 21):
        add_game(session, i, minutes=60)
    html = " ".join(client.get("/sync").text.split())
    assert "20 games in your library." in html
    assert "Due now: achievements for 20, store details for 20, review counts for 20 (about 2 minutes)" in html


def test_a_page_with_everything_up_to_date_says_so(client, session):
    add_game(session, 1, minutes=0)
    game = session.get(Game, 1)
    game.details_fetched_at = game.reviews_fetched_at = datetime.now()
    session.commit()
    assert "Nothing is due: you are up to date" in client.get("/sync").text


def test_both_buttons_are_offered_and_the_second_asks_before_re_reading_everything(client, configured, session):
    html = client.get("/sync").text
    assert 'action="/sync/start"' in html and "Sync now" in html
    assert 'name="full" value="1"' in html and "Check everything again" in html and "confirm(" in html


def test_syncing_and_finding_games_never_run_at_once(client, configured):
    # a search is running: a sync must wait
    web_app.discover_job._snapshot = JobSnapshot(state="running")
    post(client, "/sync/start")
    assert "Finding games is running" in client.get("/sync").text and not client.held
    web_app.discover_job._snapshot = JobSnapshot()
    # a sync is running: a search must wait
    post(client, "/sync/start")
    assert len(client.held) == 1
    post(client, "/explore/find", source="popular")
    assert len(client.held) == 1 and "A sync is running" in client.get("/explore").text


def test_the_sync_buttons_cannot_be_pressed_from_another_website(client, configured):
    for path in ("/sync/start", "/sync/stop"):
        response = client.post(path, headers={"Origin": "https://evil.example"}, follow_redirects=False)
        assert response.status_code == 403
    assert not client.held


def test_the_library_count_separates_owned_games_from_family_sharing(client, session):
    for i in (1, 2, 3):
        add_game(session, i, minutes=60)
    add_game(session, 4, owned=False, family=True)
    add_game(session, 5, owned=False, family=True)
    due = what_is_due(session)
    assert (due["library"], due["owned"], due["family"]) == (5, 3, 2)
    assert "5 games in your library (3 owned, 2 from Family Sharing)" in client.get("/sync").text


def test_games_steam_cannot_give_achievements_for_are_described_honestly(client, session):
    add_game(session, 1, minutes=600)  # played, never read
    html = client.get("/sync").text
    assert "Steam sometimes cannot give a few games' achievements" in html
    assert "have no achievements read yet" not in html
