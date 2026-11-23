"""Thin client for the Steam Web API."""

import re
from dataclasses import dataclass

import requests

OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
PLAYER_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
CHANGED_APPS_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"


class SteamError(Exception):
    pass


@dataclass(frozen=True)
class OwnedGame:
    app_id: int
    name: str
    playtime_total_min: int
    playtime_recent_min: int
    last_played_ts: int = 0  # unix seconds; 0 = never

    @property
    def playtime_total_hours(self) -> float:
        return self.playtime_total_min / 60

    @property
    def playtime_recent_hours(self) -> float:
        return self.playtime_recent_min / 60


def parse_profile(text: str) -> tuple[str, str] | None:
    """What was typed for "your Steam account": a 17-digit SteamID, a profile address with one, or a
    profile address / custom name ("steamcommunity.com/id/name"). ("id", digits), ("name", custom
    name), or None when it is none of these."""
    text = text.strip().strip("/")
    if re.fullmatch(r"\d{17}", text):
        return "id", text
    if m := re.search(r"steamcommunity\.com/profiles/(\d{17})", text):
        return "id", m.group(1)
    if m := re.search(r"steamcommunity\.com/id/([A-Za-z0-9_-]{2,32})", text):
        return "name", m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{2,32}", text):
        return "name", text
    return None


def _get(url: str, params: dict, timeout: float) -> dict:
    try:
        resp = requests.get(url, params=params | {"format": "json"}, timeout=timeout)
    except requests.RequestException as exc:
        raise SteamError(f"Could not reach Steam: {exc}") from exc
    if resp.status_code in (401, 403):
        raise SteamError("Steam rejected the API key. Check that you pasted all of it.")
    if resp.status_code != 200:
        raise SteamError(f"Steam returned HTTP {resp.status_code}.")
    return resp.json().get("response", {})


def resolve_steam_id(api_key: str, kind_and_value: tuple[str, str], timeout: float = 15) -> str:
    """The 17-digit SteamID for what parse_profile read (a custom name is looked up on Steam)."""
    kind, value = kind_and_value
    if kind == "id":
        return value
    found = _get(VANITY_URL, {"key": api_key, "vanityurl": value}, timeout)
    if found.get("success") != 1 or not found.get("steamid"):
        raise SteamError(f"Steam has no profile named \"{value}\". Paste the address of your profile instead.")
    return str(found["steamid"])


def fetch_player_name(api_key: str, steam_id: str, timeout: float = 15) -> str:
    """The account's display name; it proves the key works and the SteamID exists."""
    players = _get(PLAYER_URL, {"key": api_key, "steamids": steam_id}, timeout).get("players") or []
    if not players:
        raise SteamError("Steam has no account with that SteamID.")
    return players[0].get("personaname") or steam_id


@dataclass(frozen=True)
class AppChange:
    """One entry of Steam's store change list."""

    app_id: int
    last_modified: int  # unix seconds: when its store info or price last changed
    price_change_number: int | None


def fetch_changed_apps(api_key: str, since: int, timeout: float = 60) -> list[AppChange]:
    """The games and software whose store info or price changed after `since` (unix seconds), from
    Valve's documented IStoreService/GetAppList (it needs your key and takes `if_modified_since`)."""
    out: list[AppChange] = []
    last_appid = 0
    for _ in range(20):  # 50,000 an answer: a month of changes is about one answer
        answer = _get(
            CHANGED_APPS_URL,
            {
                "key": api_key,
                "if_modified_since": since,
                "include_games": "true",
                "include_software": "true",
                "max_results": 50_000,
                "last_appid": last_appid,
            },
            timeout,
        )
        out.extend(
            AppChange(a["appid"], a.get("last_modified") or 0, a.get("price_change_number"))
            for a in answer.get("apps") or []
            if a.get("appid")
        )
        if not answer.get("have_more_results") or not answer.get("last_appid"):
            break
        last_appid = answer["last_appid"]
    return out


def fetch_owned_games(api_key: str, steam_id: str, timeout: float = 15) -> list[OwnedGame]:
    """Return every game on the account, including free-to-play ones."""
    try:
        resp = requests.get(
            OWNED_GAMES_URL,
            params={
                "key": api_key,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
                # Steam hides "unvetted" apps (new or niche games) unless asked.
                "skip_unvetted_apps": "false",
                "format": "json",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SteamError(f"Could not reach Steam: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SteamError("Steam rejected the API key (check STEAM_API_KEY).")
    if resp.status_code != 200:
        raise SteamError(f"Steam returned HTTP {resp.status_code}.")

    data = resp.json().get("response", {})
    # A private profile (or a wrong SteamID) comes back as an empty response.
    if "games" not in data:
        raise SteamError(
            "No games returned. Make sure STEAM_ID is the 64-bit ID and that "
            "'Game details' is set to Public in your Steam privacy settings."
        )

    return [
        OwnedGame(
            app_id=g["appid"],
            name=g.get("name", f"App {g['appid']}"),
            playtime_total_min=g.get("playtime_forever", 0),
            playtime_recent_min=g.get("playtime_2weeks", 0),
            last_played_ts=g.get("rtime_last_played", 0),
        )
        for g in data["games"]
    ]
