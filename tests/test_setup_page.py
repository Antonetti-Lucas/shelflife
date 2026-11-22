"""The first-run page: connect your Steam account from the app instead of editing .env. Steam is faked."""

import pytest
from fastapi.testclient import TestClient

from shelflife import envfile
from shelflife.steam import OwnedGame, SteamError, parse_profile
from shelflife.web import app as web_app

SAME_SITE = {"Origin": "http://testserver"}
SECRET = "KEY-abc123SECRET"
ID = "76561198000000001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temporary .env, and the key variables restored afterwards."""
    path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_PATH", path)
    for name in ("STEAM_API_KEY", "STEAM_ID", "STEAM_COUNTRY"):
        monkeypatch.setenv(name, "")
    return path


@pytest.fixture
def steam(monkeypatch):
    """Fake Steam: the answers, and the calls made."""
    state = {"calls": [], "names": {"gaben": ID}, "library": [OwnedGame(1, "A", 0, 0)], "bad_key": False}

    def resolve(key, parsed, timeout=15):
        state["calls"].append(("resolve", key, parsed))
        kind, value = parsed
        if kind == "id":
            return value
        if value not in state["names"]:
            raise SteamError(f'Steam has no profile named "{value}". Paste the address of your profile instead.')
        return state["names"][value]

    def player(key, steam_id, timeout=15):
        state["calls"].append(("player", key, steam_id))
        if state["bad_key"]:
            raise SteamError("Steam rejected the API key. Check that you pasted all of it.")
        return "Gabe"

    def games(key, steam_id, timeout=15):
        if isinstance(state["library"], Exception):
            raise state["library"]
        return state["library"]

    monkeypatch.setattr(web_app, "resolve_steam_id", resolve)
    monkeypatch.setattr(web_app, "fetch_player_name", player)
    monkeypatch.setattr(web_app, "fetch_owned_games", games)
    return state


@pytest.fixture
def client():
    with TestClient(web_app.app) as c:
        yield c


def post(client, **data):
    return client.post("/setup", data=data, headers=SAME_SITE, follow_redirects=False)


@pytest.mark.parametrize(
    "text, parsed",
    [
        (ID, ("id", ID)),
        (f"https://steamcommunity.com/profiles/{ID}/", ("id", ID)),
        ("https://steamcommunity.com/id/gaben/", ("name", "gaben")),
        ("steamcommunity.com/id/gaben", ("name", "gaben")),
        ("gaben", ("name", "gaben")),
        ("  gaben  ", ("name", "gaben")),
        ("not a profile!!", None),
        ("", None),
        ("1234", ("name", "1234")),
    ],
)
def test_what_you_may_paste_for_your_account(text, parsed):
    assert parse_profile(text) == parsed


def test_the_page_explains_the_steps_and_every_other_page_points_to_it(client, env):
    html = client.get("/setup").text
    assert "Steam Web API key" in html and 'name="api_key"' in html and 'name="profile"' in html
    assert "setup-banner" not in html  # no banner on the page itself
    assert "setup-banner" in client.get("/library").text  # not connected: every page offers it


def test_a_good_key_and_profile_are_saved_and_work_at_once(client, env, steam):
    from shelflife.config import try_load_settings

    html = post(client, api_key=SECRET, profile="https://steamcommunity.com/id/gaben", country="br").text
    assert "Connected as Gabe: 1 games found." in html and 'href="/sync"' in html
    saved = env.read_text(encoding="utf-8")
    assert f"STEAM_API_KEY={SECRET}" in saved and f"STEAM_ID={ID}" in saved and "STEAM_COUNTRY=BR" in saved
    settings = try_load_settings()
    assert settings is not None and settings.steam_api_key == SECRET and settings.steam_id == ID  # no restart
    assert "setup-banner" not in client.get("/library").text


def test_the_key_is_never_shown_back(client, env, steam):
    post(client, api_key=SECRET, profile=ID)
    assert SECRET not in client.get("/setup").text and "A key is already saved" in client.get("/setup").text
    assert SECRET not in post(client, api_key="", profile=ID).text


def test_other_lines_of_the_env_file_are_kept(client, env, steam):
    env.write_text("# my notes\nSTEAM_API_KEY=old\nSOMETHING_ELSE=1\n", encoding="utf-8")
    post(client, api_key=SECRET, profile=ID)
    saved = env.read_text(encoding="utf-8").splitlines()
    assert saved[0] == "# my notes" and "SOMETHING_ELSE=1" in saved and f"STEAM_API_KEY={SECRET}" in saved
    assert "STEAM_API_KEY=old" not in saved


@pytest.mark.parametrize(
    "data, says",
    [
        ({"api_key": "", "profile": ID}, "Paste your Steam Web API key"),
        ({"api_key": SECRET, "profile": ""}, "Paste your profile address"),
        ({"api_key": SECRET, "profile": "no good!!"}, "Paste your profile address"),
        ({"api_key": SECRET, "profile": ID, "country": "USA"}, "two letters"),
        ({"api_key": SECRET, "profile": "https://steamcommunity.com/id/nobody"}, 'Steam has no profile named'),
    ],
)
def test_bad_input_is_explained_and_nothing_is_saved(client, env, steam, data, says):
    response = post(client, **data)
    assert response.status_code == 400 and says in response.text
    assert not env.exists() and SECRET not in response.text


def test_a_rejected_key_saves_nothing(client, env, steam):
    steam["bad_key"] = True
    response = post(client, api_key=SECRET, profile=ID)
    assert response.status_code == 400 and "rejected the API key" in response.text and not env.exists()


def test_a_private_profile_is_saved_with_a_hint(client, env, steam):
    steam["library"] = SteamError("No games returned. Make sure 'Game details' is set to Public in your Steam privacy settings.")
    html = post(client, api_key=SECRET, profile=ID).text
    assert "Saved for Gabe" in html and "Public" in html and f"STEAM_ID={ID}" in env.read_text(encoding="utf-8")


def test_blank_fields_keep_what_is_saved(client, env, steam):
    post(client, api_key=SECRET, profile=ID, country="US")
    steam["calls"].clear()
    html = post(client, api_key="", profile="", country="").text
    assert "Connected as Gabe" in html and ("player", SECRET, ID) in steam["calls"]


def test_a_value_with_a_line_break_cannot_be_written(env):
    with pytest.raises(ValueError):
        envfile.update_env({"STEAM_ID": "1\nINJECT=1"}, env)


def test_other_sites_cannot_post_to_the_page(client, env, steam):
    response = client.post("/setup", data={"api_key": SECRET, "profile": ID}, headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert response.status_code in (400, 403) and not env.exists()
