"""Games that are not on Steam, from IGDB: found, logged, tagged, taught, and left alone by Sync."""

import pytest
from fastapi.testclient import TestClient

from shelflife import diary, igdb, otherplatforms, recommend
from shelflife.models import Game, LibraryEntry
from shelflife.play import is_played
from shelflife.recommend import score_all
from shelflife.steam import OwnedGame
from shelflife.sync import (
    about_due, achievements_due, details_due, length_due, player_hours_due, prices_due, reviews_due,
    tags_due, upsert_library, wikidata_due,
)
from shelflife.web import app as web_app
from tests.conftest import FakeResponse
from tests.factories import add_game, build_scenario

SAME_SITE = {"Origin": "http://testserver"}

# The shape IGDB answers in (fields as asked in igdb.FIELDS), trimmed.
WARPED = {
    "id": 1187, "name": " Crash  Bandicoot: Warped ", "game_type": 0, "first_release_date": 910051200,
    "url": "https://www.igdb.com/games/crash-bandicoot-warped", "summary": "Time-travel platforming.",
    "storyline": "Cortex returns.", "total_rating": 84.27, "total_rating_count": 579,
    "cover": {"id": 1, "image_id": "co873w"},
    "platforms": [{"id": 7, "name": "PlayStation"}, {"id": 9, "name": "PlayStation 3"}],
    "genres": [{"id": 8, "name": "Platform"}, {"id": 31, "name": "Adventure"}],
    "themes": [{"id": 1, "name": "Action"}, {"id": 18, "name": "Science fiction"}],
    "player_perspectives": [{"id": 2, "name": "Third person"}],
    "game_modes": [{"id": 1, "name": "Single player"}],
    "keywords": [{"id": 1, "name": "time travel"}, {"id": 2, "name": "psone classics"}, {"id": 3, "name": "Dinosaurs"}],
    "external_games": [{"id": 5, "uid": "12909", "external_game_source": 3}],
}
HOLLOW = {
    "id": 14593, "name": "Hollow Knight", "game_type": 0, "total_rating_count": 900,
    "platforms": [{"id": 6, "name": "PC (Microsoft Windows)"}],
    "external_games": [{"id": 9, "uid": "367520", "external_game_source": 1}],
}


def found(item=WARPED) -> igdb.IgdbGame:
    return igdb.parse(item)


@pytest.fixture
def client():
    with TestClient(web_app.app) as c:
        yield c


def post(client, path, **data):
    return client.post(path, data=data, headers=SAME_SITE, follow_redirects=False)


# ---- reading IGDB's answers ----


def test_an_answer_is_read_into_a_game():
    g = found()
    assert (g.id, g.name, g.year, g.steam_id) == (1187, "Crash Bandicoot: Warped", 1998, None)
    assert g.release_date == "3 Nov, 1998"  # written as Steam writes dates, so the pages read the year
    assert g.cover_url.endswith("/t_cover_big/co873w.jpg")
    assert g.text == "Time-travel platforming.\n\nCortex returns."
    assert g.platforms == ["PlayStation", "PlayStation 3"] and g.rating == 84.3


def test_a_steam_link_in_the_answer_gives_the_steam_id():
    assert found(HOLLOW).steam_id == 367520


def test_tags_are_mapped_in_order_and_keywords_only_when_they_are_steam_tags():
    known = {"Time Travel", "Dinosaurs", "Platformer"}
    assert igdb.mapped_tags(found(), known) == [
        "Platformer", "Adventure",  # genres
        "3D Platformer", "Action-Adventure",  # what two names make together: Platform + Third person...
        "Time Travel", "Dinosaurs",  # keywords that are Steam tags ("psone classics" is not)
        "Action", "Sci-fi", "Third Person", "Singleplayer",  # themes, perspective, mode
    ]


def test_only_the_first_few_keyword_tags_are_kept():
    many = dict(WARPED, keywords=[{"id": i, "name": f"k{i}"} for i in range(10)])
    tags = igdb.mapped_tags(found(many), {f"K{i}" for i in range(10)})
    assert [t for t in tags if t.startswith("K")] == [f"K{i}" for i in range(igdb.MAX_KEYWORD_TAGS)]


def test_every_igdb_genre_theme_perspective_and_mode_has_a_mapping():
    # The names IGDB listed on 2026-11-26; a new one would map to nothing until added to TAG_MAP.
    for name in ("Point-and-click", "Hack and slash/Beat 'em up", "Card & Board Game", "Science fiction",
                 "4X (explore, expand, exploit, and exterminate)", "Bird view / Isometric", "Split screen"):
        assert name in igdb.TAG_MAP


def test_tags_in_order_get_the_vote_shares_of_a_typical_steam_game():
    votes = igdb.votes_for(["RPG", " Fantasy ", "RPG", "Open  World"])
    assert votes == {"RPG": 1000, "Fantasy": 924, "Open World": 854}
    assert len(igdb.votes_for([f"t{i}" for i in range(40)])) == igdb.MAX_TAGS
    assert igdb.ordered_tags(votes) == ["RPG", "Fantasy", "Open World"]


def test_search_answers_put_the_exact_name_and_the_well_known_game_first():
    games = [
        igdb.IgdbGame(1, "Super Mario 64", rating_count=13),
        igdb.IgdbGame(2, "Super Mario 64 DS", rating_count=220),
        igdb.IgdbGame(3, "Super Mario 64", rating_count=2102),
        igdb.IgdbGame(4, "Mario Kart", rating_count=5000),
    ]
    assert [g.id for g in igdb.rank("super mario 64", games)] == [3, 1, 2, 4]


# ---- talking to IGDB ----


def test_search_gets_a_token_once_then_asks_igdb(monkeypatch):
    monkeypatch.setenv("IGDB_CLIENT_ID", "cid")
    monkeypatch.setenv("IGDB_CLIENT_SECRET", "secret")
    igdb._token.clear()
    calls = []

    def fake_post(url, params=None, data=None, headers=None, timeout=None):
        calls.append((url, data))
        if "twitch" in url:
            return FakeResponse(200, {"access_token": "tok", "expires_in": 5000000})
        assert headers["Client-ID"] == "cid" and headers["Authorization"] == "Bearer tok"
        return FakeResponse(200, [WARPED, HOLLOW])

    monkeypatch.setattr(igdb.requests, "post", fake_post)
    first = igdb.search('Crash "Warped"')
    igdb.search("Hollow")
    assert [c[0] for c in calls].count(igdb.TOKEN_URL) == 1  # the token is kept in memory
    assert b'search "Crash  Warped "' in calls[1][1] and b"game_type = (0,4,8,9,10,11)" in calls[1][1]
    assert [g.name for g in first] == ["Hollow Knight", "Crash Bandicoot: Warped"]  # by how many rated it
    igdb._token.clear()


def test_without_keys_igdb_says_it_is_not_set_up():
    assert not igdb.configured()
    with pytest.raises(igdb.NotConfigured):
        igdb.search("Crash")


# ---- logging one ----


def test_a_game_not_on_steam_joins_the_library_with_its_own_id_and_tags(session):
    add_game(session, 1, "Steam game", tags={"Time Travel": 10, "Platformer": 5})
    game = otherplatforms.add_igdb_game(session, found(), "PlayStation", 12)
    assert game.steam_app_id == -1187 and not game.on_steam and game.type == "game"
    entry = session.get(LibraryEntry, -1187)
    assert (entry.platform, entry.playtime_total_min) == ("PlayStation", 720) and is_played(entry, False)
    assert game.tags == igdb.votes_for(
        ["Platformer", "Adventure", "3D Platformer", "Action-Adventure", "Time Travel", "Action", "Sci-fi", "Third Person", "Singleplayer"]
    )
    assert game.about_text.startswith("Time-travel") and game.cover_url.endswith("co873w.jpg")
    assert game.igdb["url"].endswith("crash-bandicoot-warped") and game.release_date == "3 Nov, 1998"
    assert otherplatforms.where_you_have_it(game) == "on PlayStation"


def test_without_hours_it_goes_on_the_backlog(session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation 3")
    assert not is_played(session.get(LibraryEntry, -1187), False)


def test_its_platform_must_be_one_igdb_lists_or_other(session):
    with pytest.raises(ValueError):
        otherplatforms.add_igdb_game(session, found(), "Xbox")
    otherplatforms.add_igdb_game(session, found(), "Other")
    otherplatforms.set_platform(session, -1187, "PlayStation 3")
    with pytest.raises(ValueError):
        otherplatforms.set_platform(session, -1187, "Nintendo Switch")


def test_it_cant_be_added_twice_and_can_be_removed_keeping_your_rating(session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 3)
    with pytest.raises(ValueError, match="already in your library"):
        otherplatforms.add_igdb_game(session, found(), "PlayStation")
    diary.save_rating(session, -1187, 4.5)
    otherplatforms.remove_game(session, -1187)
    assert session.get(LibraryEntry, -1187) is None and diary.load_ratings(session)[-1187] == 4.5


def test_an_igdb_game_that_is_on_steam_is_added_as_the_steam_game(session):
    game = otherplatforms.add_igdb_game(session, found(HOLLOW), "PlayStation", 30)
    assert game.steam_app_id == 367520 and game.library_entry.platform == "PlayStation"
    assert session.get(Game, -14593) is None


def test_a_negative_id_is_not_a_steam_game(session):
    with pytest.raises(ValueError):
        otherplatforms.add_game(session, -5, "x", "PlayStation")


# ---- tags you change ----


def test_your_tags_replace_igdbs_and_survive_a_refresh(session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 5)
    igdb.set_tags(session, -1187, ["Collectathon", "3D Platformer", "Platformer"])
    game = session.get(Game, -1187)
    assert game.tags_edited and igdb.ordered_tags(game.tags) == ["Collectathon", "3D Platformer", "Platformer"]
    igdb.upsert(session, found())  # IGDB read again
    assert igdb.ordered_tags(game.tags) == ["Collectathon", "3D Platformer", "Platformer"]
    igdb.reset_tags(session, -1187)
    assert not game.tags_edited and igdb.ordered_tags(game.tags)[0] == "Platformer"


def test_a_steam_games_tags_cant_be_changed_here(session):
    add_game(session, 5, tags={"RPG": 10})
    with pytest.raises(ValueError):
        igdb.set_tags(session, 5, ["Puzzle"])


# ---- Sync leaves it alone ----


def test_sync_never_asks_steam_about_it(session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 5)
    game = session.get(Game, -1187)
    game.details_fetched_at = game.about_text = None  # even with nothing read, nothing is due for it
    game.tags_fetched_at = None
    session.commit()
    for due in (tags_due(session), details_due(session), about_due(session), reviews_due(session),
                player_hours_due(session), length_due(session), prices_due(session), wikidata_due(session)):
        assert -1187 not in {g.steam_app_id for g in due}
    assert -1187 not in {e.steam_app_id for e in achievements_due(session)}


def test_reading_your_steam_library_keeps_it(session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 5)
    upsert_library(session, [OwnedGame(app_id=10, name="Steam game", playtime_total_min=60, playtime_recent_min=0, last_played_ts=0)])
    assert session.get(LibraryEntry, -1187).platform == "PlayStation"


# ---- the taste ----


def test_a_rated_game_not_on_steam_teaches_the_taste(session):
    ratings = build_scenario(session)  # likes Stealth; 40 and 41 are Stealth games you don't own
    stealthy = dict(WARPED, genres=[], themes=[{"id": 23, "name": "Stealth"}], keywords=[], game_modes=[], player_perspectives=[])
    otherplatforms.add_igdb_game(session, found(stealthy), "PlayStation", 20)
    diary.save_rating(session, -1187, 5.0)
    model, scores = recommend._cached(session, ratings | {-1187: 5.0}, {})
    assert -1187 in model.likes  # it teaches like any rated game
    assert scores.by_id[40].similarity > scores.by_id[21].similarity  # stealth still beats sports


def test_one_you_took_out_of_your_library_is_never_suggested(session):
    ratings = build_scenario(session)
    stealthy = dict(WARPED, themes=[{"id": 23, "name": "Stealth"}])
    otherplatforms.add_igdb_game(session, found(stealthy), "PlayStation")
    assert -1187 in score_all(session, ratings).backlog  # unplayed and yours: backlog, like any game
    otherplatforms.remove_game(session, -1187)
    after = score_all(session, ratings)
    assert -1187 not in after.discover | after.backlog | after.family


# ---- pages ----


def test_its_page_credits_igdb_has_no_steam_links_and_lets_you_edit_tags(client, session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 5)
    page = client.get("/game/-1187").text
    assert "Not on Steam." in page and "crash-bandicoot-warped" in page and "IGDB rating 84/100" in page
    assert "store.steampowered.com/app/-1187" not in page and 'data-price-id="-1187"' not in page
    assert 'action="/game/-1187/tags"' in page and "Save tags" in page and '<option value="PlayStation 3"' in page


def test_saving_tags_from_the_page_keeps_their_order(client, session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 5)
    resp = client.post("/game/-1187/tags", data={"tag": ["Puzzle", "Platformer"]}, headers=SAME_SITE, follow_redirects=False)
    assert resp.status_code == 303
    session.expire_all()
    assert igdb.ordered_tags(session.get(Game, -1187).tags) == ["Puzzle", "Platformer"]
    assert "Back to IGDB" in client.get("/game/-1187").text
    post(client, "/game/-1187/tags", reset="1")
    session.expire_all()
    assert not session.get(Game, -1187).tags_edited


def test_the_add_page_searches_igdb_and_tells_steam_games_apart(client, session, monkeypatch):
    monkeypatch.setattr(igdb, "search", lambda q: [found(), found(HOLLOW)])
    page = client.get("/library/add?q=crash&where=igdb").text
    assert 'action="/library/add-igdb"' in page and 'name="igdb_id" value="1187"' in page
    assert "Not on Steam · PlayStation, PlayStation 3" in page
    assert "On Steam too" in page and 'name="app_id" value="367520"' in page


def test_the_add_page_says_when_igdb_is_not_set_up(client):
    page = client.get("/library/add?q=crash&where=igdb").text
    assert "IGDB_CLIENT_ID" in page


def test_adding_from_the_page_reads_the_game_from_igdb_again(client, session, monkeypatch):
    asked = []
    monkeypatch.setattr(igdb, "fetch", lambda i: asked.append(i) or found())
    resp = post(client, "/library/add-igdb", igdb_id="1187", platform="PlayStation", hours="2,5", next="/library/add?where=igdb")
    assert resp.status_code == 303 and asked == [1187]
    session.expire_all()
    assert session.get(LibraryEntry, -1187).playtime_total_min == 150


def test_its_poster_is_igdbs_cover(session):
    otherplatforms.add_igdb_game(session, found(), "PlayStation", 5)
    assert web_app.poster_url(session.get(Game, -1187)).endswith("co873w.jpg")


def test_explore_never_lists_a_game_not_on_steam(client, session):
    build_scenario(session)
    otherplatforms.add_igdb_game(session, found(dict(WARPED, themes=[{"id": 23, "name": "Stealth"}])), "PlayStation")
    otherplatforms.remove_game(session, -1187)
    assert "/game/-1187" not in client.get("/explore").text
    assert "/game/-1187" not in client.get("/explore?like=1").text


def test_the_short_description_ends_on_a_whole_sentence():
    assert igdb.short("One. Two.") == "One. Two."
    long = "First sentence here. " + "word " * 80
    assert igdb.short(long) == "First sentence here."
    assert igdb.short("x" * 10 + " " + "y " * 200).endswith("…")
    assert igdb.short("") is None
