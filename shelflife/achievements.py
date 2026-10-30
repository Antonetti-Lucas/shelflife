"""Per-game achievement progress from the Steam Web API."""

from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from shelflife.steam import SteamError
from shelflife.throttle import Throttle

PLAYER_URL = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
GLOBAL_URL = (
    "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/"
)

steam_throttle = Throttle(0.3)


class AchievementsUnavailable(SteamError):
    """Steam can't report achievements for this one game (it errors every time)."""


@dataclass(frozen=True)
class AchievementStats:
    total: int  # 0 = the game has no achievements
    unlocked: int
    rarest_unlocked_pct: float | None
    first_unlock_at: datetime | None
    last_unlock_at: datetime | None
    unlock_times: tuple[int, ...] = ()  # unix seconds, oldest first


NO_ACHIEVEMENTS = AchievementStats(0, 0, None, None, None)


def _get(url: str, params: dict, timeout: float) -> requests.Response:
    steam_throttle.wait()
    try:
        return requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SteamError(f"Could not reach Steam: {exc}") from exc


def fetch_achievements(
    api_key: str, steam_id: str, app_id: int, timeout: float = 15
) -> AchievementStats:
    """Your achievement progress for one game (needs public game details)."""
    resp = _get(
        PLAYER_URL, {"key": api_key, "steamid": steam_id, "appid": app_id}, timeout
    )
    # Steam answers 400 "Requested app has no stats" for games without achievements.
    if resp.status_code == 400:
        return NO_ACHIEVEMENTS
    if resp.status_code in (401, 403):
        raise SteamError("Steam rejected the request (check key and privacy settings).")
    if resp.status_code == 500:
        raise AchievementsUnavailable(f"Steam returned an internal error for app {app_id}.")
    if resp.status_code != 200:
        raise SteamError(f"Steam returned HTTP {resp.status_code} for app {app_id}.")

    stats = resp.json().get("playerstats", {})
    achievements = stats.get("achievements") or []
    if not achievements:
        return NO_ACHIEVEMENTS

    held = [a for a in achievements if a.get("achieved")]
    stamps = sorted(a["unlocktime"] for a in held if a.get("unlocktime"))
    times = [datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None) for t in stamps]
    rarest = _rarest_unlocked_pct(app_id, {a["apiname"] for a in held}, timeout) if held else None
    return AchievementStats(
        total=len(achievements),
        unlocked=len(held),
        rarest_unlocked_pct=rarest,
        first_unlock_at=min(times) if times else None,
        last_unlock_at=max(times) if times else None,
        unlock_times=tuple(stamps),
    )


def _rarest_unlocked_pct(app_id: int, held: set[str], timeout: float) -> float | None:
    """Lowest global unlock rate among achievements you hold (public endpoint)."""
    resp = _get(GLOBAL_URL, {"gameid": app_id}, timeout)
    if resp.status_code != 200:
        return None
    rows = resp.json().get("achievementpercentages", {}).get("achievements", [])
    rates = [float(r["percent"]) for r in rows if r["name"] in held]
    return min(rates) if rates else None
