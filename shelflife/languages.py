"""The languages a game can be played in, from the Steam store, and whether it fits yours."""

import html
import re

AUDIO = "audio"  # full audio in this language
TEXT = "text"  # interface or subtitles only

_MARKUP = re.compile(r"<[^>]*>")

# Offered first on the My taste page; any other language the store lists can be picked too.
COMMON = (
    "English", "Portuguese - Brazil", "Spanish - Latin America", "Spanish - Spain", "French", "German",
    "Italian", "Japanese", "Korean", "Simplified Chinese", "Traditional Chinese", "Russian", "Polish",
    "Portuguese - Portugal", "Turkish",
)


def parse_languages(raw: str | None) -> dict[str, str]:
    """The store's `supported_languages` text as {language: "audio" or "text"}.

    It looks like "English<strong>*</strong>, French, Portuguese - Brazil<br><strong>*</strong>
    languages with full audio support": a star marks full audio, and the note after the line
    break isn't a language."""
    if not raw:
        return {}
    listed = re.split(r"<br\s*/?>", raw, maxsplit=1, flags=re.I)[0]
    out: dict[str, str] = {}
    for part in listed.split(","):
        audio = "*" in part
        name = " ".join(html.unescape(_MARKUP.sub("", part)).replace("*", "").split())
        if name:
            out[name] = AUDIO if audio else TEXT
    return out


def fits(game_languages: dict[str, str] | None, wanted: list[str] | tuple[str, ...]) -> bool:
    """Whether a game can be played in one of `wanted`. No languages chosen, or none known for
    the game yet, means it fits: a game is never hidden for lack of information."""
    if not wanted or not game_languages:
        return True
    return any(lang in game_languages for lang in wanted)
