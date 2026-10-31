"""Filtering games by community tag, for the tag box in Library, Explore and Diary.

A game "has" a tag when it is among its TOP_TAGS most voted ones, so a tag a
single user added to a game does not count. Names match whole and ignore case.
"""

from collections import Counter
from collections.abc import Iterable

TOP_TAGS = 20


def parse_tags(raw: str | None) -> list[str]:
    """"Roguelike, co-op" -> ["Roguelike", "co-op"]: comma separated, no repeats."""
    seen: set[str] = set()
    out: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def top_tag_names(tags: dict[str, int] | None) -> dict[str, str]:
    """lower-case name -> name as written, for a game's most voted tags."""
    ranked = sorted((tags or {}).items(), key=lambda kv: kv[1], reverse=True)[:TOP_TAGS]
    return {name.lower(): name for name, _ in ranked}


def has_all(tags: dict[str, int] | None, wanted: list[str]) -> bool:
    """Whether a game has every wanted tag (trivially true for no tags)."""
    if not wanted:
        return True
    have = top_tag_names(tags)
    return all(w.lower() in have for w in wanted)


class TagIndex:
    """The tags that exist in a set of games: suggestions for the box, and a way
    to tell "no game has this tag" from "no game matches all your filters"."""

    def __init__(self, all_tags: Iterable[dict[str, int] | None]):
        self._counts: Counter[str] = Counter()
        self._spelling: dict[str, str] = {}
        for tags in all_tags:
            for key, name in top_tag_names(tags).items():
                self._counts[key] += 1
                self._spelling.setdefault(key, name)

    def popular(self, limit: int = 300) -> list[str]:
        """Tag names, the ones on the most games first (ties alphabetical)."""
        ranked = sorted(self._counts, key=lambda k: (-self._counts[k], k))
        return [self._spelling[k] for k in ranked[:limit]]

    def unknown(self, wanted: list[str]) -> list[str]:
        """Wanted tags that no game in the set has."""
        return [w for w in wanted if w.lower() not in self._counts]
