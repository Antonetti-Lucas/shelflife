"""How long a game is, estimated from how long its reviewers played it (Game.player_hours).

Reviewers' median hours run about 1.24 times HowLongToBeat's "main + extras" time: measured on 92
of the owner's games, the estimate below typically lands within 16% of it (3 in 4 within 26%;
PLANS.md section 6). HowLongToBeat itself isn't used. A game that never ends (multiplayer, sandbox,
survival...) has no length to finish; for those it's how long players typically play.
"""

from dataclasses import dataclass

LENGTH_FROM_REVIEWERS = 1.24  # reviewers' median hours / HLTB main + extras
# A game whose top 3 tags include one of these has no ending to reach.
ENDLESS_TAGS = frozenset({
    "Massively Multiplayer", "MMORPG", "PvP", "Multiplayer", "Sandbox", "Survival", "Idler", "Clicker",
    "Battle Royale", "MOBA", "Open World Survival Craft", "Automation", "Online Co-Op", "Team-Based",
    "eSports", "Competitive", "Hero Shooter", "Party Game", "Physics",
})
ENDLESS_TOP = 3

# The length filter: key -> (label, from hours, under hours).
BANDS = {
    "short": ("Under 5 h", 0.0, 5.0),
    "medium": ("5 to 15 h", 5.0, 15.0),
    "long": ("15 to 40 h", 15.0, 40.0),
    "huge": ("Over 40 h", 40.0, float("inf")),
}


@dataclass(frozen=True)
class Length:
    hours: float
    endless: bool  # True: how long players typically play, not a length to finish

    @property
    def short_text(self) -> str:
        """"~12 h", for a card."""
        if self.hours < 1:
            return "<1 h"
        return f"~{self.hours:.0f} h" if self.hours >= 2 else f"~{self.hours:.1f} h"

    @property
    def text(self) -> str:
        """The sentence for a game page."""
        if self.endless:
            return f"Players typically play {self.short_text.lstrip('~')}"
        return f"About {self.short_text.lstrip('~')} to finish, with some extras"


def is_endless(tags: dict[str, int] | None) -> bool:
    top = sorted((tags or {}).items(), key=lambda kv: kv[1], reverse=True)[:ENDLESS_TOP]
    return any(name in ENDLESS_TAGS for name, _ in top)


def estimate(game) -> Length | None:
    """The game's length, or None when its reviewers' hours haven't been read (or were too few)."""
    hours = game.player_hours
    if not hours or not game.player_hours_sample:
        return None
    median = hours[2]
    if is_endless(game.tags):
        return Length(median, True)
    return Length(median / LENGTH_FROM_REVIEWERS, False)


def in_band(length: Length | None, band: str) -> bool:
    """Whether a game is in a length band; any game is when `band` is not one. A game whose length
    isn't known is left out of every band."""
    if band not in BANDS:
        return True
    if length is None:
        return False
    _, low, high = BANDS[band]
    return low <= length.hours < high
