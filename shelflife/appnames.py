"""Bulk app names from Valve's documented IStoreService/GetAppList."""

import requests

from shelflife.steam import SteamError
from shelflife.throttle import Throttle

APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
PAGE_SIZE = 50_000  # the maximum

app_list_throttle = Throttle(1.0)


def fetch_app_names(api_key: str, wanted: set[int], timeout: float = 60) -> dict[int, str]:
    """Names for the wanted app ids.

    The list is paged in ascending app id order, so paging stops as soon as it
    passes the largest id we need. Ids Steam doesn't list are simply absent.
    """
    if not wanted:
        return {}
    last_needed = max(wanted)
    names: dict[int, str] = {}
    last_app_id = 0
    while True:
        app_list_throttle.wait()
        try:
            resp = requests.get(
                APP_LIST_URL,
                params={
                    "key": api_key,
                    "include_games": "true",
                    "include_software": "true",
                    "include_dlc": "false",
                    "include_videos": "false",
                    "include_hardware": "false",
                    "max_results": PAGE_SIZE,
                    "last_appid": last_app_id,
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise SteamError(f"Could not reach Steam: {exc}") from exc
        if resp.status_code in (401, 403):
            raise SteamError("Steam rejected the API key (check STEAM_API_KEY).")
        if resp.status_code != 200:
            raise SteamError(f"Steam returned HTTP {resp.status_code} for the app list.")

        data = resp.json().get("response", {})
        for app in data.get("apps", []):
            if app["appid"] in wanted:
                names[app["appid"]] = app["name"]
        last_app_id = data.get("last_appid", 0)
        if not data.get("have_more_results") or last_app_id >= last_needed:
            return names
