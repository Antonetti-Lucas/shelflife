"""The languages a game can be played in: read from the store, chosen on My taste, and used to
leave out games you couldn't play."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from shelflife import store, sync
from shelflife.languages import AUDIO, TEXT, fits, parse_languages
from shelflife.models import Game, Setting
from shelflife.recommend import score_all
from shelflife.settings import get_languages, set_languages
from shelflife.web.app import app
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}
STEAM_TEXT = (
    "English<strong>*</strong>, French<strong>*</strong>, Portuguese - Brazil, Simplified Chinese"
    "<br><strong>*</strong>languages with full audio support"
)


# ---- reading the store's list ----


def test_the_store_text_becomes_languages_with_audio_marked():
    assert parse_languages(STEAM_TEXT) == {
        "English": AUDIO, "French": AUDIO, "Portuguese - Brazil": TEXT, "Simplified Chinese": TEXT,
    }


@pytest.mark.parametrize("raw", [None, "", "<br><strong>*</strong>languages with full audio support"])
def test_no_list_means_no_languages(raw):
    assert parse_languages(raw) == {}


def test_a_game_fits_when_it_has_one_of_your_languages_or_nothing_is_known():
    game = {"English": AUDIO, "Polish": TEXT}
    assert fits(game, ["Portuguese - Brazil", "English"])
    assert not fits(game, ["Portuguese - Brazil"])
    assert fits(None, ["Portuguese - Brazil"]) and fits({}, ["Portuguese - Brazil"])  # unknown: never hidden
    assert fits(game, [])  # none chosen: no filter


# ---- the setting ----


def test_your_languages_are_remembered(session):
    assert get_languages(session) == []
    set_languages(session, ["English", " Portuguese - Brazil ", "English", ""])
    assert get_languages(session) == ["English", "Portuguese - Brazil"]


def test_a_damaged_setting_means_no_filter(session):
    session.add(Setting(key="languages", value="not json"))
    session.commit()
    assert get_languages(session) == []


# ---- leaving games out ----


@pytest.fixture
def scenario(session):
    ratings = build_scenario(session)
    session.get(Game, 40).languages = {"Polish": TEXT}  # Pool Stealth Good: Polish only
    session.get(Game, 41).languages = {"English": AUDIO}
    session.commit()  # 20, 21, 30 and the rest: not read yet
    return ratings


def test_recommendations_leave_out_games_without_your_languages(session, scenario):
    assert 40 in score_all(session, scenario).discover
    set_languages(session, ["English"])
    scores = score_all(session, scenario)
    assert 40 not in scores.discover and 41 in scores.discover
    assert 20 in scores.backlog  # languages not read yet: stays


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_explore_leaves_them_out_too(client, session, scenario):
    assert "Pool Stealth Good" in client.get("/explore").text
    set_languages(session, ["English"])
    html = client.get("/explore").text
    assert "Pool Stealth Good" not in html and "Pool Stealth Bad" in html


def test_my_taste_offers_the_languages_and_saves_your_choice(client, session, scenario):
    html = client.get("/taste").text
    assert 'action="/taste/languages"' in html and 'value="Portuguese - Brazil"' in html
    assert "Polish" in html  # a language the store lists for one of your games
    client.post("/taste/languages", data={"lang": ["English", "Portuguese - Brazil"]}, headers=SAME_SITE)
    assert get_languages(session) == ["English", "Portuguese - Brazil"]
    assert 'value="English" checked' in client.get("/taste").text
    client.post("/taste/languages", data={}, headers=SAME_SITE)  # nothing ticked: no filter
    assert get_languages(session) == []


def test_the_game_page_says_when_a_game_is_not_in_your_languages(client, session, scenario):
    assert "1 language" in client.get("/game/40").text
    set_languages(session, ["English"])
    assert "Not in your languages" in client.get("/game/40").text
    assert "Not in your languages" not in client.get("/game/41").text


# ---- reading them for games fetched before ----


def details_read(session, app_id, text="", languages=None):
    game = session.get(Game, app_id)
    game.details_fetched_at, game.about_text, game.languages = datetime(2026, 1, 1), text, languages
    session.commit()


def test_the_catch_up_reads_languages_only_for_games_that_could_be_recommended(session):
    add_game(session, 1, "Played", minutes=3000)
    add_game(session, 2, "Backlog")
    add_game(session, 3, "Pool", owned=False)
    add_game(session, 4, "Known", owned=False)
    for app_id in (1, 2, 3):
        details_read(session, app_id)
    details_read(session, 4, languages={"English": TEXT})
    assert [g.steam_app_id for g in sync.about_due(session)] == [2, 3]


def test_the_catch_up_stores_the_languages(session, monkeypatch):
    add_game(session, 2, "Backlog")
    details_read(session, 2)
    monkeypatch.setattr(store, "fetch_store_details", lambda app_id, timeout=15: store.StoreDetails(
        type="game", short_description="d", about_text="words", languages={"English": AUDIO}))
    assert sync.sync_about(session) == 1
    assert session.get(Game, 2).languages == {"English": AUDIO} and sync.about_due(session) == []
