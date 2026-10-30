"""Thin client for the Steam Web API."""

from dataclasses import dataclass

import requests

OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"


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
