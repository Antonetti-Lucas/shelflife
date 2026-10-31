"""The store's full text, and a taste learned from your games (tags and words)."""

from datetime import datetime

import pytest
import requests

from shelflife import learned, store, sync
from shelflife.diary import save_rating
from shelflife.learned import fit, likeness, text_index, text_vectors, unit
from shelflife.models import Game, LibraryEntry
from shelflife.recommend import _because, _build_model, engagement_fraction, explain, recommend, score_all
from shelflife.settings import set_taste_mode
from shelflife.sync import about_due, enrich_one, sync_about
from shelflife.throttle import RateLimited
from shelflife.wishlist import WishlistEntry, import_wishlist
from tests.conftest import FakeResponse
from tests.factories import add_game

NOW = datetime(2026, 6, 1)


def with_text(session, app_id, text, details=True):
    game = session.get(Game, app_id)
    game.about_text = text
    if details:
        game.details_fetched_at = NOW
    session.commit()


# ---- reading the text ----


def test_the_stores_html_becomes_plain_text():
    raw = '<h2 class="bb_tag">Welcome</h2><img src="https://x.test/a.png"> Fight &amp; run!<br><a href="https://x.test">see https://x.test/y</a>\n\n  end'
    assert store.clean_store_text(raw) == "Welcome Fight & run! see end"
    assert store.clean_store_text(None) == ""


def test_the_about_text_comes_with_the_store_details(monkeypatch):
    body = {"7": {"success": True, "data": {"type": "game", "name": "Seven", "short_description": "d", "about_the_game": "<p>A <b>long</b> story</p>", "detailed_description": "ignored"}}}
    monkeypatch.setattr(store.requests, "get", lambda url, **kw: FakeResponse(200, body))
    assert store.fetch_store_details(7).about_text == "A long story"


@pytest.fixture
def fake_store(monkeypatch):
    pages, fetched = {}, []

    def details(app_id, timeout=15):
        fetched.append(app_id)
        if isinstance(pages.get(app_id), Exception):
            raise pages[app_id]
        if app_id not in pages:
            return None
        return store.StoreDetails(type="game", short_description="d", name="N", about_text=pages[app_id])

    monkeypatch.setattr(store, "fetch_store_details", details)
    monkeypatch.setattr(sync, "_fill_tags", lambda game, spy=None: None)
    details.pages, details.fetched = pages, fetched
    return details


def test_enriching_a_game_keeps_its_text_and_a_missing_page_leaves_an_empty_one(session, fake_store):
    fake_store.pages[1] = "Sneak past guards"
    one, two = add_game(session, 1, type=None), add_game(session, 2, type=None)
    enrich_one(one)
    enrich_one(two)  # the store has no page for 2
    assert session.get(Game, 1).about_text == "Sneak past guards" and session.get(Game, 2).about_text == ""


def test_a_store_hiccup_does_not_wipe_a_text_that_was_read(session, fake_store):
    add_game(session, 1, type="game")
    with_text(session, 1, "kept")
    enrich_one(session.get(Game, 1))  # no page this time
    assert session.get(Game, 1).about_text == "kept"


# ---- catching up the games read before the text was kept ----


def test_games_with_details_but_no_text_are_due_yours_first_then_the_wishlist_then_the_pool(session):
    add_game(session, 1, "Played", minutes=600)
    add_game(session, 2, "Barely", minutes=5)
    add_game(session, 10, "Wished", owned=False)
    add_game(session, 4, "Pool", owned=False)
    add_game(session, 5, "Has text", minutes=100)
    add_game(session, 6, "No details yet", minutes=100)
    add_game(session, 7, "A DLC", minutes=100, type="dlc")
    add_game(session, 8, "Empty on purpose", minutes=100)
    import_wishlist(session, [WishlistEntry(10, 0, None)])
    for app_id in (1, 2, 10, 4, 5, 7, 8):
        with_text(session, app_id, None)
    with_text(session, 5, "has some")
    with_text(session, 8, "")
    assert [g.steam_app_id for g in about_due(session)] == [1, 2, 10, 4]


def test_the_catch_up_reads_only_the_text_and_marks_a_missing_page(session, fake_store):
    fake_store.pages[1] = "The words"
    for app_id in (1, 2):
        add_game(session, app_id, f"G{app_id}", minutes=100)
        with_text(session, app_id, None)
    done = sync_about(session)
    assert done == 2 and fake_store.fetched == [1, 2]
    assert session.get(Game, 1).about_text == "The words" and session.get(Game, 2).about_text == ""
    assert session.get(Game, 1).type == "game" and about_due(session) == []  # nothing else touched, nothing left


def test_the_catch_up_can_be_stopped_and_carries_on_later(session, fake_store):
    for app_id in (1, 2, 3):
        fake_store.pages[app_id] = f"text {app_id}"
        add_game(session, app_id, minutes=100 - app_id)
        with_text(session, app_id, None)
    calls = []
    assert sync_about(session, should_stop=lambda: calls.append(1) or len(calls) > 1) == 1
    assert [g.steam_app_id for g in about_due(session)] == [2, 3]
    assert sync_about(session) == 2


def test_the_catch_up_stops_when_the_store_turns_it_away_and_keeps_what_it_read(session, fake_store):
    fake_store.pages[1] = "ok"
    fake_store.pages[2] = RateLimited("slow down")
    for app_id in (1, 2):
        add_game(session, app_id, minutes=100 - app_id)
        with_text(session, app_id, None)
    with pytest.raises(RateLimited):
        sync_about(session)
    assert session.get(Game, 1).about_text == "ok"


def test_a_few_network_failures_are_skipped_but_three_in_a_row_stop_it(session, fake_store):
    for app_id in (1, 2, 3, 4):
        fake_store.pages[app_id] = requests.ConnectionError("down")
        add_game(session, app_id, minutes=100 - app_id)
        with_text(session, app_id, None)
    with pytest.raises(Exception, match="3 failures in a row"):
        sync_about(session)


# ---- words ----


def test_games_that_share_rare_words_are_alike_and_a_word_only_one_game_uses_counts_for_nothing():
    vectors = text_vectors(
        {
            1: "stealth heist vault thieves crew",
            2: "heist vault crew robbery",
            3: "farming harvest crops village",
            4: "farming crops animals barn",
            5: "unique-to-one zzzz",
        }
    )
    assert likeness({}, vectors[1], {}, vectors[2], 1.0) > 0.3
    assert likeness({}, vectors[1], {}, vectors[3], 1.0) == 0.0
    assert "zzzz" not in vectors[5] and "unique-to-one" not in vectors[5]


def test_the_word_index_is_rebuilt_when_a_text_changes(session):
    add_game(session, 1, tags={"A": 1})
    add_game(session, 2, tags={"A": 1})
    add_game(session, 3, tags={"A": 1})
    with_text(session, 1, "alpha beta gamma")
    with_text(session, 2, "alpha delta epsilon")
    with_text(session, 3, "zulu yankee xray")
    first = text_index(session)
    assert set(first[1]) == {"alpha"}
    with_text(session, 2, "beta gamma delta")
    assert set(text_index(session)[1]) >= {"beta", "gamma"}
    assert text_index(session) is text_index(session)  # unchanged: the same one again


# ---- the fit ----


def vec(**tags):
    return unit(dict(tags))


def test_one_game_you_loved_is_enough_to_pull_close_games_up_and_leave_unrelated_ones_at_zero():
    tags = {1: vec(stealth=1.0), 2: vec(stealth=1.0, action=0.3), 3: vec(racing=1.0)}
    model = fit([(1, 0.9)], tags, {}, beta=0.0)
    assert model.predict(tags[2], None) > 0.1 and model.predict(tags[3], None) == 0.0


def test_games_like_the_ones_you_disliked_score_below_zero():
    tags = {1: vec(stealth=1.0), 2: vec(sports=1.0), 3: vec(sports=1.0, multiplayer=0.4), 4: vec(stealth=1.0, action=0.4)}
    model = fit([(1, 0.9), (2, -0.8)], tags, {}, beta=0.0)
    assert model.predict(tags[3], None) < 0 < model.predict(tags[4], None)


def test_a_favourite_stays_close_however_many_other_games_you_like():
    """The old profile averaged every game together; each game is learned on its own now."""
    tags = {0: vec(beatemup=1.0, crime=0.8), 1000: vec(beatemup=0.9, crime=0.7, detective=0.3)}
    training = [(0, 1.0)]
    for i in range(1, 41):  # forty other loved games about other things
        tags[i] = vec(**{f"topic{i % 8}": 1.0, f"flavour{i}": 0.5})
        training.append((i, 0.8))
    for i in range(41, 61):
        tags[i] = vec(**{f"topic{i % 8}": 1.0})
    tags[2000] = vec(topic3=1.0, flavour9=0.5)
    model = fit(training, tags, {}, beta=0.0)
    ranked = sorted((g for g in tags if g not in dict(training)), key=lambda g: -model.predict(tags[g], None))
    assert ranked[0] == 1000


def test_words_find_a_game_the_tags_cannot():
    tags = {1: vec(a=1.0), 2: vec(b=1.0), 3: vec(c=1.0)}
    words = text_vectors({1: "ninja clan shadow blade", 2: "ninja clan shadow blade assassin", 3: "farming harvest crops village"})
    model = fit([(1, 1.0)], tags, words, beta=0.5)
    assert model.predict(tags[2], words[2]) > model.predict(tags[3], words[3]) == 0.0


# ---- in the recommendations ----


@pytest.fixture
def liked_and_candidates(session):
    add_game(session, 1, "Loved Ninja", tags={"Zeta": 100}, minutes=3000)
    add_game(session, 2, "Other Loved", tags={"Eta": 100}, minutes=3000)
    add_game(session, 3, "Ninja Cousin", tags={"Theta": 100}, owned=False, reviews=(9000, 500))
    add_game(session, 4, "Unrelated", tags={"Iota": 100}, owned=False, reviews=(9000, 500))
    add_game(session, 5, "Filler", tags={"Kappa": 100}, minutes=600)
    return {1: 5.0, 2: 4.0}


def test_two_games_with_different_tags_but_the_same_story_are_matched_by_their_text(session, liked_and_candidates):
    for app_id, text in {
        1: "A shinobi clan betrayed, a blade, a long road of revenge in feudal Japan",
        2: "Raise crops and animals on a quiet farm",
        3: "A shinobi betrayed by his clan takes up the blade for revenge across feudal Japan",
        4: "Race supercars across the coast",
        5: "Solve puzzles in a tower",
    }.items():
        with_text(session, app_id, text)
    result = recommend(session, liked_and_candidates, limit=10).discover
    assert [r.game.name for r in result][0] == "Ninja Cousin"
    assert result[0].similarity > 0.05 and result[1].similarity == 0.0


def test_without_any_text_the_tags_alone_decide(session, liked_and_candidates):
    add_game(session, 6, "Tag Cousin", tags={"Zeta": 100}, owned=False, reviews=(9000, 500))
    result = recommend(session, liked_and_candidates, limit=10).discover
    assert result[0].game.name == "Tag Cousin"


def test_a_three_star_rating_teaches_that_games_like_it_are_ordinary(session):
    add_game(session, 1, "Loved", tags={"Zeta": 100}, minutes=3000)
    add_game(session, 2, "Meh", tags={"Eta": 100}, minutes=3000)
    add_game(session, 3, "Like Loved", tags={"Zeta": 100, "Theta": 20}, owned=False, reviews=(9000, 500))
    add_game(session, 4, "Like Meh", tags={"Eta": 100, "Theta": 20}, owned=False, reviews=(9000, 500))
    set_taste_mode(session, "ratings")
    scores = score_all(session, {1: 5.0, 2: 3.0}).by_id
    assert scores[3].similarity > 0 > scores[4].similarity  # like your usual 3-star game: a little below what you love
    assert (2, 0.0) in _build_model(session, {1: 5.0, 2: 3.0}, {}).training


def test_in_ratings_only_mode_games_you_played_but_never_rated_are_not_learned_from(session):
    add_game(session, 1, "Rated", tags={"Zeta": 100}, minutes=600)
    add_game(session, 2, "Played, not rated", tags={"Eta": 100}, minutes=60000)
    set_taste_mode(session, "ratings")
    assert [i for i, _ in _build_model(session, {1: 5.0}, {}).training] == [1]
    set_taste_mode(session, "mix")
    assert {i for i, _ in _build_model(session, {1: 5.0}, {}).training} == {1, 2}


def test_the_mix_learns_from_seventy_percent_rating_and_thirty_percent_play(session):
    add_game(session, 1, "Rated and played", tags={"Zeta": 100}, minutes=60000)
    set_taste_mode(session, "mix")
    (_, weight), = _build_model(session, {1: 4.0}, {}).training
    assert weight == pytest.approx(0.7 * 0.5 + 0.3 * engagement_fraction(session.get(LibraryEntry, 1)))


def test_the_reasons_are_worked_out_only_when_read(session, liked_and_candidates, monkeypatch):
    add_game(session, 6, "Tag Cousin", tags={"Zeta": 100}, owned=False, reviews=(9000, 500))
    calls = []
    real = _because
    monkeypatch.setattr("shelflife.recommend._because", lambda *a: calls.append(1) or real(*a))
    rec = explain(session, liked_and_candidates, {}, 6)
    assert calls == []
    assert rec.because == list(rec.because) and len(calls) == 1
    assert list(rec.because) and len(calls) == 1  # once


def test_the_reasons_name_the_games_you_liked_that_a_pick_is_closest_to(session, liked_and_candidates):
    add_game(session, 6, "Tag Cousin", tags={"Zeta": 100}, owned=False, reviews=(9000, 500))
    rec = explain(session, liked_and_candidates, {}, 6)
    assert rec.because and rec.because[0].startswith("Loved Ninja (rated 5")


def test_what_was_learned_is_kept_until_your_ratings_change(session, liked_and_candidates, monkeypatch):
    fits = []
    real = learned.fit
    monkeypatch.setattr(learned, "fit", lambda *a, **k: fits.append(1) or real(*a, **k))
    score_all(session, liked_and_candidates)
    score_all(session, liked_and_candidates)
    assert len(fits) == 1
    save_rating(session, 5, 2.0)
    score_all(session, {**liked_and_candidates, 5: 2.0})
    assert len(fits) == 2


def test_a_game_with_no_tags_scores_zero_instead_of_failing(session, liked_and_candidates):
    add_game(session, 7, "No tags", tags={}, owned=False)
    assert 7 not in score_all(session, liked_and_candidates).by_id


def test_no_ratings_means_no_taste_and_no_failure(session):
    add_game(session, 1, "Candidate", tags={"Zeta": 100}, owned=False)
    assert recommend(session, {}, limit=10).discover[0].similarity == 0.0


def test_similar_games_use_the_words_too(session):
    from shelflife.similar import similar_to, tag_idf

    add_game(session, 1, "Reference", tags={"Zeta": 100, "Eta": 50}, owned=False)
    add_game(session, 2, "Same words", tags={"Zeta": 100, "Theta": 80}, owned=False)
    add_game(session, 3, "Other words", tags={"Zeta": 100, "Theta": 80}, owned=False)
    add_game(session, 4, "Filler", tags={"Iota": 100}, owned=False)
    for app_id, text in {1: "clockwork city thieves guild", 2: "a guild of thieves in a clockwork city", 3: "race cars on tracks", 4: "grow crops"}.items():
        with_text(session, app_id, text)
    ref = session.get(Game, 1)
    games = [session.get(Game, i) for i in (2, 3)]
    without = {r.game.name: r.similarity for r in similar_to(ref, games, tag_idf(session))}
    with_words = {r.game.name: r.similarity for r in similar_to(ref, games, tag_idf(session), text_index(session))}
    assert without["Same words"] == without["Other words"]
    assert with_words["Same words"] > with_words["Other words"]


def test_the_command_line_can_catch_up_the_descriptions(session, fake_store, monkeypatch, capsys):
    import sys

    from scripts import sync as script

    fake_store.pages[1] = "words"
    add_game(session, 1, "G1", minutes=100)
    with_text(session, 1, None)
    monkeypatch.setattr(sys, "argv", ["sync", "--descriptions"])
    assert script.main() == 0
    assert "Read the text of 1 games" in capsys.readouterr().out
    session.expire_all()
    assert session.get(Game, 1).about_text == "words"
