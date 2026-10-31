"""Settings loaded from the environment (.env file supported)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    steam_api_key: str
    steam_id: str


def try_load_settings() -> Settings | None:
    """The key and SteamID from .env, or None when either is missing (the web app uses
    this: it must explain a missing setting on the page, not exit)."""
    key = os.getenv("STEAM_API_KEY", "").strip()
    steam_id = os.getenv("STEAM_ID", "").strip()
    return Settings(steam_api_key=key, steam_id=steam_id) if key and steam_id else None


def load_settings() -> Settings:
    key = os.getenv("STEAM_API_KEY", "").strip()
    steam_id = os.getenv("STEAM_ID", "").strip()
    missing = [
        name
        for name, value in (("STEAM_API_KEY", key), ("STEAM_ID", steam_id))
        if not value
    ]
    if missing:
        # Every caller is a command-line script, so exit with a plain message
        # instead of a traceback.
        raise SystemExit(
            f"Error: missing {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in (see the Setup section of the README)."
        )
    return Settings(steam_api_key=key, steam_id=steam_id)
