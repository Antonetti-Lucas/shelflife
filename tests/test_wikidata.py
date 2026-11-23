"""Wikidata: series and first release dates for Steam games. The query service is faked: no network."""

from datetime import datetime, timedelta

import pytest
import requests
from fastapi.testclient import TestClient

from shelflife import diary, series, wikidata
from shelflife.models import Game
from shelflife.series import first_year, series_of
from shelflife.sync import WIKIDATA_REFRESH_DAYS, sync_wikidata, wikidata_due
from shelflife.sync_job import run_sync, summarize_sync
from shelflife.throttle import RateLimited
from shelflife.web.app import app, release_year
from shelflife.wikidata import BATCH, WikidataGame, fetch_games, parse
from tests.factories import add_game
from tests.test_sync_button import owned, steam  # noqa: F401 (the fake Steam fixture)


def row(steam_id, item="Q1", series_id=None, label=None, ordinal=None, pub=None):
    out = {"steam": {"value": str(steam_id)}, "item": {"value": f"http://www.wikidata.org/entity/{item}"}}
    if series_id:
        out["series"] = {"value": f"http://www.wikidata.org/entity/{series_id}"}
        out["seriesLabel"] = {"value": label or series_id}
    if ordinal:
        out["ordinal"] = {"value": ordinal}
    if pub:
        out["pub"] = {"value": f"{pub}T00:00:00Z"}
    return out


# ---- reading the answer ----


def test_a_row_per_series_and_date_becomes_one_game_with_the_earliest_date():
    found = parse([
        row(10, "Q10", "Q99", "Hitman", "3", "2012-11-20"),
        row(10, "Q10", "Q99", "Hitman", "3", "2012-11-19"),  # another platform
        row(10, "Q10", "Q5", "Stealth games", None, "2013-01-01"),
    ])
    game = found[10]
    assert game.item == "Q10" and game.released == "2012-11-19"
    assert game.series == [{"id": "Q99", "label": "Hitman", "ordinal": "3"}, {"id": "Q5", "label": "Stealth games", "ordinal": None}]


def test_a_series_nobody_named_is_dropped_and_a_game_without_series_is_kept():
    found = parse([row(1, "Q1", "Q123", "Q123"), row(2, "Q2")])
    assert found[1].series == [] and found[2].series == [] and set(found) == {1, 2}


def test_a_late_ordinal_fills_a_missing_one_and_junk_rows_are_skipped():
    found = parse([row(1, "Q1", "Q9", "S", None), row(1, "Q1", "Q9", "S", "2"), {"steam": {"value": "x"}, "item": {"value": "a/Q1"}}])
    assert found[1].series[0]["ordinal"] == "2" and list(found) == [1]


# ---- asking ----


class FakeWeb:
    def __init__(self, status=200, bindings=(), headers=None):
        self.status, self.bindings, self.headers, self.calls = status, list(bindings), headers or {}, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        outer = self

        class Response:
            status_code = outer.status
            headers = outer.headers

            def raise_for_status(self):
                if outer.status >= 400:
                    raise requests.HTTPError(str(outer.status))

            def json(self):
                return {"results": {"bindings": outer.bindings}}

        return Response()


def test_the_query_names_the_steam_ids_and_the_tool_and_sends_no_email(monkeypatch):
    web = FakeWeb(bindings=[row(10, "Q10", "Q99", "Hitman", "1", "2000-11-19")])
    monkeypatch.setattr(wikidata.requests, "get", web)
    found = fetch_games([10, 11])
    url, params, headers = web.calls[0]
    assert url == wikidata.URL and '"10" "11"' in params["query"] and "P1733" in params["query"]
    assert "ShelfLife" in headers["User-Agent"] and "@" not in headers["User-Agent"]  # names the tool, no personal address
    assert found[10].series[0]["label"] == "Hitman" and 11 not in found


def test_too_many_games_for_one_query_is_refused():
    with pytest.raises(ValueError):
        fetch_games(list(range(BATCH + 1)))


@pytest.mark.parametrize("status", [429, 503])
def test_being_told_to_slow_down_stops_with_the_wait_in_the_message(monkeypatch, status):
    monkeypatch.setattr(wikidata.requests, "get", FakeWeb(status, headers={"Retry-After": "40"}))
    with pytest.raises(RateLimited, match="40 seconds"):
        fetch_games([1])


def test_another_error_is_raised(monkeypatch):
    monkeypatch.setattr(wikidata.requests, "get", FakeWeb(500))
    with pytest.raises(requests.HTTPError):
        fetch_games([1])


# ---- what is due, and the sync step ----


def asked(session, app_id, days_ago=1, data=None):
    game = session.get(Game, app_id)
    game.wikidata, game.wikidata_fetched_at = data if data is not None else {}, datetime.now() - timedelta(days=days_ago)
    session.commit()


def test_games_never_asked_or_asked_long_ago_are_due_library_first(session):
    add_game(session, 1, "Pool", owned=False)
    add_game(session, 2, "Mine")
    add_game(session, 3, "Fresh")
    add_game(session, 4, "Old")
    add_game(session, 5, "A DLC", type="dlc")
    asked(session, 3)
    asked(session, 4, days_ago=WIKIDATA_REFRESH_DAYS + 5)
    assert [g.steam_app_id for g in wikidata_due(session)] == [2, 4, 1]
    assert [g.steam_app_id for g in wikidata_due(session, everything=True)] == [2, 3, 4, 1]


def test_the_answers_are_saved_and_a_game_without_an_item_is_remembered_as_asked(session, monkeypatch):
    add_game(session, 1, "Known")
    add_game(session, 2, "Unknown")
    monkeypatch.setattr(wikidata, "fetch_games", lambda ids, timeout=90: {1: WikidataGame("Q1", [{"id": "Q9", "label": "S", "ordinal": "1"}], "2001-02-03")})
    assert sync_wikidata(session) == 2
    assert session.get(Game, 1).wikidata == {"item": "Q1", "series": [{"id": "Q9", "label": "S", "ordinal": "1"}], "released": "2001-02-03"}
    assert session.get(Game, 2).wikidata == {} and session.get(Game, 2).wikidata_fetched_at is not None
    assert wikidata_due(session) == []  # not asked again until the refresh time


def test_games_are_asked_in_batches_and_progress_is_reported(session, monkeypatch):
    for i in range(1, BATCH + 6):
        add_game(session, i, f"G{i}")
    sizes, progress = [], []
    monkeypatch.setattr(wikidata, "fetch_games", lambda ids, timeout=90: sizes.append(len(ids)) or {})
    sync_wikidata(session, on_progress=lambda i, n, g: progress.append((i, n)))
    assert sizes == [BATCH, 5] and progress[-1] == (BATCH + 5, BATCH + 5)


def test_stopping_between_batches_keeps_what_was_saved(session, monkeypatch):
    for i in range(1, BATCH + 6):
        add_game(session, i, f"G{i}")
    calls = []
    monkeypatch.setattr(wikidata, "fetch_games", lambda ids, timeout=90: calls.append(1) or {})
    sync_wikidata(session, should_stop=lambda: len(calls) >= 1)
    assert len(calls) == 1 and len(wikidata_due(session)) == 5


def test_a_sync_runs_the_step_last_and_says_what_it_did(session, steam, monkeypatch):
    steam.library = [owned(1, minutes=600)]
    monkeypatch.setattr(wikidata, "fetch_games", lambda ids, timeout=90: {i: WikidataGame("Q1") for i in ids})
    steps = []
    result = run_sync(session, "key", "id", on_progress=lambda step, *a: steps.append(step))
    assert result.series == 1 and steps[-1] == 11
    assert "series and first release dates for 1 games" in summarize_sync(result).message


@pytest.mark.parametrize("failure", [RateLimited("slow down"), requests.ConnectionError("down")])
def test_wikidata_being_unreachable_does_not_make_the_sync_fail(session, steam, monkeypatch, failure):
    steam.library = [owned(1, minutes=600)]

    def fail(ids, timeout=90):
        raise failure

    monkeypatch.setattr(wikidata, "fetch_games", fail)
    result = run_sync(session, "key", "id")
    assert result.aborted is None and result.series == 0 and result.series_note
    assert "Wikidata could not be asked" in summarize_sync(result).message and result.details == 1


# ---- series ----


def with_wikidata(session, app_id, name, series_list, released=None, **kw):
    game = add_game(session, app_id, name, **kw)
    game.wikidata = {"item": f"Q{app_id}", "series": series_list, "released": released}
    game.wikidata_fetched_at = datetime.now()
    session.commit()
    return game


def hitman(label="Hitman"):
    return lambda n: {"id": "Q19308", "label": label, "ordinal": n}


@pytest.fixture(autouse=True)
def fresh_series_index():
    series._index_cache.update(key=None, members={})


def test_a_series_lists_its_games_in_the_series_own_order(session):
    with_wikidata(session, 1, "Blood Money", [hitman()("4")], "2006-05-30")
    with_wikidata(session, 2, "Codename 47", [hitman()("1")], "2000-11-19")
    with_wikidata(session, 3, "Absolution", [hitman()("5")], "2012-11-20")
    [found] = series_of(session, session.get(Game, 1))
    assert found.label == "Hitman" and [m.game.name for m in found.members] == ["Codename 47", "Blood Money", "Absolution"]
    assert [m.year for m in found.members] == [2000, 2006, 2012]


def test_without_numbers_for_every_game_the_order_is_by_first_release(session):
    with_wikidata(session, 1, "Late", [hitman()(None)], "2012-01-01")
    with_wikidata(session, 2, "Early", [hitman()("9")], "2001-01-01")  # only one has a number: not trusted
    assert [m.game.name for m in series_of(session, session.get(Game, 1))[0].members] == ["Early", "Late"]


def test_a_series_with_only_one_game_here_is_not_shown(session):
    with_wikidata(session, 1, "Alone", [hitman()("1")], "2000-01-01")
    assert series_of(session, session.get(Game, 1)) == []


def test_the_most_specific_series_come_first_and_at_most_two_are_shown(session):
    big, small, mid = {"id": "Q1", "label": "Franchise", "ordinal": None}, {"id": "Q2", "label": "Sub", "ordinal": None}, {"id": "Q3", "label": "Mid", "ordinal": None}
    with_wikidata(session, 1, "A", [big, small, mid], "2001-01-01")  # in Franchise (5 games), Mid (3), Sub (2)
    with_wikidata(session, 2, "B", [big, mid], "2002-01-01")
    with_wikidata(session, 3, "C", [big, mid], "2003-01-01")
    with_wikidata(session, 4, "D", [small, big], "2004-01-01")
    with_wikidata(session, 5, "E", [big], "2005-01-01")
    assert [s.label for s in series_of(session, session.get(Game, 1))] == ["Sub", "Mid"]


def test_the_first_year_comes_from_the_date(session):
    assert first_year(with_wikidata(session, 1, "A", [], "2012-11-20")) == 2012
    assert first_year(with_wikidata(session, 2, "B", [], None)) is None and first_year(add_game(session, 3, "C")) is None


# ---- release years ----


def test_a_port_sold_years_later_counts_from_its_first_release(session):
    game = with_wikidata(session, 1, "GTA III", [], "2001-10-23")
    game.release_date = "4 Jan, 2008"
    assert release_year(game) == 2001


def test_a_game_the_store_gives_no_date_for_gets_wikidatas(session):
    game = with_wikidata(session, 1, "Sniper Elite V2", [], "2012-04-30")
    assert game.release_date is None and release_year(game) == 2012


def test_a_later_wikidata_date_never_overrides_the_store(session):
    game = with_wikidata(session, 1, "X", [], "2015-01-01")
    game.release_date = "3 Mar, 2010"
    assert release_year(game) == 2010


# ---- the game page ----


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_the_game_page_lists_the_rest_of_the_series_with_your_stars(client, session):
    with_wikidata(session, 1, "Blood Money", [hitman()("4")], "2006-05-30")
    with_wikidata(session, 2, "Codename 47", [hitman()("1")], "2000-11-19")
    with_wikidata(session, 3, "Absolution", [hitman()("5")], "2012-11-20")
    diary.save_rating(session, 2, 4.5)
    html = client.get("/game/1").text
    assert "Hitman" in html and "3 games here" in html
    section = html.split("games here")[1]
    assert section.index("Codename 47") < section.index("Absolution") and "Blood Money" not in section.split("</section>")[0]  # not itself
    assert "★" in section.split("</section>")[0]  # your rating of Codename 47


def test_no_series_block_for_a_game_in_no_series(client, session):
    with_wikidata(session, 1, "Lonely", [], "2001-01-01")
    assert "games here" not in client.get("/game/1").text


def test_the_page_says_when_a_game_was_first_released(client, session):
    game = with_wikidata(session, 1, "GTA III", [], "2001-10-23")
    game.release_date = "4 Jan, 2008"
    session.commit()
    html = client.get("/game/1").text
    assert "First released 2001" in html and "Released 4 Jan, 2008" in html


def test_the_page_shows_wikidatas_date_when_the_store_has_none(client, session):
    with_wikidata(session, 1, "Sniper Elite V2", [], "2012-04-30")
    assert "Released 2012-04-30" in client.get("/game/1").text


def test_no_first_released_note_when_the_dates_agree(client, session):
    game = with_wikidata(session, 1, "Same", [], "2010-03-03")
    game.release_date = "3 Mar, 2010"
    session.commit()
    assert "First released" not in client.get("/game/1").text
