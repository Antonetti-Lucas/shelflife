"""Saving settings to the .env file (the Steam key and SteamID the first-run page asks for)."""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"  # the project folder's .env, which config.py loads at start


def update_env(values: dict[str, str], path: Path | None = None) -> None:
    """Set these variables in the .env file (replacing a line that already sets one, else adding it, and
    starting from .env.example when there is no file yet), and in this process so they work at once."""
    path = path or ENV_PATH
    for name, value in values.items():
        if not re.fullmatch(r"[A-Z_]+", name) or any(c in value for c in "\r\n"):
            raise ValueError(f"Cannot save {name!r} to .env")
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif (path.parent / ".env.example").exists():
        lines = (path.parent / ".env.example").read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    for name, value in values.items():
        pattern = re.compile(rf"^\s*{name}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{name}={value}"
                break
        else:
            lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(values)
