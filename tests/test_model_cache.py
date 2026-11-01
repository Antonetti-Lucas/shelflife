"""The model is kept between pages and built again only when the data it is built from changes."""

from datetime import datetime

import pytest

from shelflife import diary, recommend
from shelflife.hours import set_hours
from shelflife.models import Game, LibraryEntry
from shelflife.recommend import data_version, recommend as recommend_games, score_all
from shelflife.settings import set_hide_adult, set_taste_mode
from tests.factories import add_game, build_scenario


@pytest.fixture
def builds(monkeypatch):
    """Counts how often the model is built."""
    count = []
    real = recommend._build_model
    monkeypatch.setattr(recommend, "_build_model", lambda *a, **k: count.append(1) or real(*a, **k))
    return count


def scores(session):
    return score_all(session, diary.load_ratings(session), diary.load_not_interested(session))


def test_nothing_changed_nothing_rebuilt(session, builds):
    build_scenario(session)
    scores(session)
    scores(session)
    recommend_games(session, diary.load_ratings(session), diary.load_not_interested(session))
    assert len(builds) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda s: diary.save_rating(s, 20, 4.0),
        lambda s: diary.set_not_interested(s, 40, True),
        lambda s: diary.set_favorite(s, 10, True),
        lambda s: diary.set_status(s, 21, "playing"),
        lambda s: set_taste_mode(s, "engagement"),
        lambda s: set_hide_adult(s, False),
        lambda s: add_game(s, 99, "New in the pool", tags={"Stealth": 100}, owned=False, reviews=(10, 1)),
    ],
    ids=["rating", "not interested", "favorite", "status", "taste mode", "adult setting", "new game"],
)
def test_a_change_to_what_the_model_reads_rebuilds_it(session, builds, change):
    build_scenario(session)
    scores(session)
    change(session)
    scores(session)
    assert len(builds) == 2


def test_a_sync_that_changes_playtime_or_tags_rebuilds_it(session, builds):
    build_scenario(session)
    before = data_version(session)
    session.get(LibraryEntry, 1).playtime_total_min += 30
    session.commit()
    after_play = data_version(session)
    game = session.get(Game, 40)  # as a sync does it: new tags, and when they were read
    game.tags, game.tags_fetched_at = {"Stealth": 100, "Puzzle": 80}, datetime(2026, 7, 1)
    session.commit()
    assert len({before, after_play, data_version(session)}) == 3


def test_family_hours_rebuild_it(session, builds):
    add_game(session, 30, "Family", tags={"Stealth": 100}, owned=False, family=True)
    before = data_version(session)
    set_hours(session, 30, 12.0)
    assert data_version(session) != before


def test_kept_games_still_show_their_library_entry_after_the_page_is_done(session):
    build_scenario(session)
    result = scores(session)
    session.close()  # the page's session is gone; the kept model has its own
    backlog = [result.by_id[i] for i in result.backlog]
    assert backlog and all(r.game.library_entry is not None for r in backlog)
    assert all(isinstance(reason, str) for r in backlog for reason in r.because)


def test_the_results_are_the_same_as_without_the_cache(session):
    ratings = build_scenario(session)
    kept = score_all(session, ratings, {})
    recommend.clear_cache()
    model = recommend._build_model(session, ratings, {})
    fresh = recommend._score_everything(model)
    assert {i: round(r.score, 9) for i, r in kept.by_id.items()} == {i: round(r.score, 9) for i, r in fresh.by_id.items()}
    assert (kept.backlog, kept.family, kept.discover) == (fresh.backlog, fresh.family, fresh.discover)
