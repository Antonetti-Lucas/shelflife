"""How long other players play a game: read from its reviews, kept as percentiles, and used to
judge your own hours against the game's length."""

from datetime import datetime, timedelta

import pytest
import requests

from shelflife import reviews, sync
from shelflife.models import Game, LibraryEntry
from shelflife.play import PLAYER_SAMPLE_TRUST, PlayScale, players_percentile
from shelflife.reviews import MIN_PLAYER_SAMPLE, PLAYER_PAGES, PlayerHours, fetch_player_hours, weighted_percentiles
from shelflife.throttle import RateLimited
from tests.conftest import FakeResponse
from tests.factories import add_game

# ---- reading the reviews ----


def review(hours, up=True):
    return {"author": {"playtime_forever": round(hours * 60)}, "voted_up": up}


@pytest.fixture
def review_pages(monkeypatch):
    """A review endpoint serving `review_pages.pages` (lists of reviews) one cursor at a time."""
    calls = []

    def get(url, params=None, **kwargs):
        calls.append(params)
        index = 0 if params["cursor"] == "*" else int(params["cursor"])
        pages = review_pages.pages
        page = pages[index] if index < len(pages) else []
        return FakeResponse(200, {"response": {"reviews": page, "cursor": str(index + 1)}})

    monkeypatch.setattr(reviews.requests, "get", get)
    review_pages.pages, review_pages.calls = [], calls
    return review_pages


def test_it_reads_pages_until_the_reviews_run_out(review_pages):
    review_pages.pages = [[review(h) for h in range(1, 101)], [review(h) for h in range(101, 131)]]
    hours = fetch_player_hours(1)
    assert len(review_pages.calls) == 3 and hours.sample == 130  # the third page came back empty
    assert hours.percentiles[2] == pytest.approx(65, abs=1)  # the median of 1..130 hours


def test_a_short_page_is_not_taken_for_the_last(review_pages):
    review_pages.pages = [[review(5)] * 100, [review(5)] * 44, [review(5)] * 100]
    assert fetch_player_hours(1).sample == 244


def test_it_asks_for_a_years_reviews_not_just_the_last_month(review_pages):
    review_pages.pages = [[review(5)] * 30]
    fetch_player_hours(1)
    assert review_pages.calls[0]["filter"] == "all" and review_pages.calls[0]["day_range"] == 365


def test_it_reads_at_most_five_pages(review_pages):
    review_pages.pages = [[review(10)] * 100] * 9
    assert fetch_player_hours(1).sample == 100 * PLAYER_PAGES and len(review_pages.calls) == PLAYER_PAGES


def test_reviewers_with_no_hours_on_record_are_left_out(review_pages):
    review_pages.pages = [[review(0)] * 50 + [review(10)] * 30]
    hours = fetch_player_hours(1)
    assert hours.sample == 30 and hours.percentiles[0] == 10


def test_too_few_reviewers_with_hours_say_nothing(review_pages):
    review_pages.pages = [[review(5)] * (MIN_PLAYER_SAMPLE - 1)]
    assert fetch_player_hours(1) is None


def test_being_turned_away_stops_the_reading(monkeypatch):
    monkeypatch.setattr(reviews.requests, "get", lambda *a, **k: FakeResponse(429, {}))
    with pytest.raises(RateLimited):
        fetch_player_hours(1)


# ---- positive and negative reviewers in their real proportions ----


def test_without_counts_every_reviewer_counts_the_same():
    sample = [(h, True) for h in (1, 2, 3, 4, 5)] + [(h, False) for h in (6, 7, 8, 9, 10)]
    assert weighted_percentiles(sample, None, None)[2] == 5


def test_each_side_is_weighed_by_the_games_real_share_of_positive_reviews():
    # Half the sample is negative reviewers who quit after an hour, but only 2% of the game's reviews are negative.
    sample = [(30.0, True)] * 50 + [(1.0, False)] * 50
    as_sampled = weighted_percentiles(sample, None, None)
    weighed = weighted_percentiles(sample, positive=9800, negative=200)
    assert as_sampled[1] == 1.0  # a quarter of the sample played an hour
    assert weighed[0] == 30.0  # in truth hardly anyone did: even the 10th percentile is a positive reviewer


def test_one_extreme_player_moves_only_the_top():
    sample = [(h, True) for h in range(1, 100)]
    with_idler = sample + [(20000.0, True)]
    assert weighted_percentiles(with_idler, None, None)[:4] == weighted_percentiles(sample, None, None)[:4]


# ---- placing your hours among the game's players ----


CELESTE = [5, 11, 24, 51, 124]
ENDLESS = [6, 12, 40, 161, 784]


def test_your_hours_are_placed_among_the_games_players_on_a_log_scale():
    assert players_percentile(24, CELESTE) == pytest.approx(0.5)
    assert players_percentile(11, CELESTE) == pytest.approx(0.25)
    assert 0.25 < players_percentile(16, CELESTE) < 0.5


def test_the_same_hours_mean_more_in_a_short_game_than_in_an_endless_one():
    assert players_percentile(60, CELESTE) > 0.75 > players_percentile(60, ENDLESS) > 0.5


def test_beyond_the_known_players_it_rises_slowly_and_never_passes_one():
    top = players_percentile(124, CELESTE)
    assert top == pytest.approx(0.9)
    assert top < players_percentile(300, CELESTE) < players_percentile(3000, CELESTE) == 1.0


def test_no_hours_is_the_bottom():
    assert players_percentile(0, CELESTE) == 0


# ---- in the play score ----


def entry_for(session, app_id, minutes, player_hours=None, sample=0):
    game = add_game(session, app_id, minutes=minutes)
    game.player_hours, game.player_hours_sample = player_hours, sample
    session.commit()
    return session.get(LibraryEntry, app_id)


def test_a_short_game_you_finished_counts_as_well_played(session):
    library = [entry_for(session, i, h * 60) for i, h in enumerate((20, 40, 80, 150, 300), start=1)]
    short = entry_for(session, 10, 3 * 60, player_hours=[0.5, 1, 2, 3, 4], sample=400)
    score = PlayScale(library + [short]).score(short)
    assert score.players == pytest.approx(0.75)  # three hours is a lot for a game most finish in two
    library_rank = PlayScale(library + [short])._percentile("hours", 180)
    assert library_rank < 0.2  # against your other games it ranks bottom...
    assert library_rank + 0.25 < score.parts["hours"] < 0.75  # ...so its players lift it, by up to half the way


def test_without_the_games_players_your_library_decides(session):
    library = [entry_for(session, i, h * 60) for i, h in enumerate((20, 40, 80, 150, 300), start=1)]
    short = entry_for(session, 10, 3 * 60)
    score = PlayScale(library + [short]).score(short)
    assert score.players is None and score.parts["hours"] < 0.2


def test_a_thin_sample_of_players_is_trusted_less(session):
    library = [entry_for(session, i, h * 60) for i, h in enumerate((20, 40, 80, 150, 300), start=1)]
    thin = entry_for(session, 10, 3 * 60, player_hours=[0.5, 1, 2, 3, 4], sample=20)
    full = entry_for(session, 11, 3 * 60, player_hours=[0.5, 1, 2, 3, 4], sample=500)
    scale = PlayScale(library + [thin, full])
    assert scale.score(full).parts["hours"] > scale.score(thin).parts["hours"]
    trust = 500 / (500 + PLAYER_SAMPLE_TRUST)
    assert scale.score(full).parts["hours"] == pytest.approx(trust * 0.75 + (1 - trust) * scale._percentile("hours", 180))


# ---- the sync step ----


def test_the_games_you_played_are_due_most_played_first_and_then_every_six_months(session):
    add_game(session, 1, minutes=600)
    add_game(session, 2, minutes=6000)
    add_game(session, 3, minutes=10)  # backlog: not played
    add_game(session, 4, owned=False, family=True, achievements=(1, 20))  # a family game you played
    add_game(session, 5, owned=False)  # not yours
    assert [g.steam_app_id for g in sync.player_hours_due(session)] == [2, 1, 4]
    session.get(Game, 2).player_hours_fetched_at = datetime.now() - timedelta(days=10)
    session.get(Game, 1).player_hours_fetched_at = datetime.now() - timedelta(days=400)
    session.commit()
    assert [g.steam_app_id for g in sync.player_hours_due(session)] == [1, 4]
    assert len(sync.player_hours_due(session, everything=True)) == 3


def test_the_step_keeps_the_percentiles_and_marks_games_with_too_few_reviewers(session, monkeypatch):
    add_game(session, 1, minutes=600, reviews=(900, 100))
    add_game(session, 2, minutes=600)
    asked = []

    def fetch(app_id, positive=None, negative=None):
        asked.append((app_id, positive, negative))
        return PlayerHours([1, 2, 3, 4, 5], 300) if app_id == 1 else None

    monkeypatch.setattr(sync.reviews, "fetch_player_hours", fetch)
    assert sync.sync_player_hours(session) == 2
    assert (1, 900, 100) in asked  # the game's counts, to weigh the two sides
    one, two = session.get(Game, 1), session.get(Game, 2)
    assert one.player_hours == [1, 2, 3, 4, 5] and one.player_hours_sample == 300
    assert two.player_hours is None and two.player_hours_sample == 0 and two.player_hours_fetched_at is not None
    assert sync.player_hours_due(session) == []  # neither is asked again for six months


def test_a_few_network_failures_are_skipped_but_three_in_a_row_stop_the_step(session, monkeypatch):
    for i in range(1, 6):
        add_game(session, i, minutes=600 * i)
    monkeypatch.setattr(sync.reviews, "fetch_player_hours", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(sync.SteamError):
        sync.sync_player_hours(session)


def test_the_game_page_shows_your_hours_against_the_games_players(session):
    from fastapi.testclient import TestClient

    from shelflife.web.app import app

    for i, h in enumerate((20, 40, 80), start=1):
        add_game(session, i, tags={"Zeta": 100}, minutes=h * 60)
    game = add_game(session, 10, "Short One", tags={"Zeta": 100}, minutes=180)
    game.player_hours, game.player_hours_sample = [0.5, 1, 2, 3, 4], 400
    session.commit()
    with TestClient(app) as client:
        html = client.get("/game/10").text
    assert "Against this game's own players" in html and "more than <strong>75%</strong> of the 400 reviewers" in html
