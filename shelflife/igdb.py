"""Games that are not on Steam, from IGDB (https://www.igdb.com, Twitch's game database).

You log them by hand, like games from other platforms (shelflife/otherplatforms.py): search IGDB,
pick the game and the platform you played it on, type your hours. A game IGDB links to a Steam id
is added as that Steam game. Any other becomes a game of its own, with minus its IGDB id as its
id (Steam ids are positive), so it can't be added twice. Steam never reads such a game: it has no
store page, price, achievements, Steam reviews or Steam player hours.

It teaches your taste like any game, through the two things likeness compares (shelflife/learned.py):
- its text: IGDB's summary and storyline, kept where a Steam game keeps its "About this game";
- its tags: Steam's community tags carry votes, and a tag counts by its share of the game's top
  tag's votes (shelflife/tags.py). IGDB's genres, themes, perspectives, modes and keywords are
  mapped to Steam tag names (TAG_MAP, COMBOS; keywords only when one is a Steam tag's very name),
  put in order (mapped_tags), and given the vote shares a typical Steam game has at each rank (TYPICAL_SHARES).
  You can change them on the game page (set_tags), and then nothing fetched replaces them.
How good the game is (Steam's review score, shelflife/quality.py) is unknown for now: IGDB's
rating is kept, and the rework of the algorithms (item 19) decides how it should count.

Terms: IGDB is free for non-commercial use under the Twitch Developer Services Agreement; IGDB's
own docs say storing its data locally is allowed. The data stays in your own (git-ignored)
database and the game page credits and links IGDB. Needs a Twitch developer app: IGDB_CLIENT_ID
and IGDB_CLIENT_SECRET in .env. What is sent to IGDB: the names you search for and IGDB ids.
"""

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

import shelflife.config  # noqa: F401  (reads .env, where the IGDB keys are)
from shelflife.models import Game
from shelflife.throttle import RateLimited, Throttle

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.igdb.com/v4"
COVER_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/{}.jpg"  # 264x374, portrait
STEAM_SOURCE = 1  # IGDB's external_game_source for Steam
SEARCH_LIMIT = 12
SEARCH_POOL = 50  # IGDB's own search order puts ports and fan games first, so ask for more and re-rank
igdb_throttle = Throttle(0.3)  # IGDB allows 4 requests a second

# IGDB's game types worth logging: a main game, a bundle (compilations people play as one game:
# Super Mario 3D All-Stars, the owner asked 2026-11-27), a remake, remaster, port, expanded or
# standalone game. Left out: DLC, expansions, mods, episodes, seasons, updates, packs, forks.
GAME_TYPES = frozenset({0, 3, 4, 8, 9, 10, 11})

FIELDS = (
    "name,game_type,first_release_date,url,summary,storyline,total_rating,total_rating_count,"
    "cover.image_id,platforms.name,genres.name,themes.name,player_perspectives.name,game_modes.name,"
    "keywords.name,external_games.external_game_source,external_games.uid"
)

# IGDB's names -> Steam tag names. Every IGDB genre, theme, perspective and mode is here
# (checked 2026-11-26); one with no Steam tag like it maps to nothing.
TAG_MAP: dict[str, tuple[str, ...]] = {
    # genres
    "Point-and-click": ("Point & Click",),
    "Fighting": ("Fighting",),
    "Shooter": ("Shooter",),
    "Music": ("Music",),
    "Platform": ("Platformer",),
    "Puzzle": ("Puzzle",),
    "Racing": ("Racing",),
    "Real Time Strategy (RTS)": ("RTS", "Strategy"),
    "Role-playing (RPG)": ("RPG",),
    "Simulator": ("Simulation",),
    "Sport": ("Sports",),
    "Strategy": ("Strategy",),
    "Turn-based strategy (TBS)": ("Turn-Based Strategy", "Strategy"),
    "Tactical": ("Tactical",),
    "Hack and slash/Beat 'em up": ("Hack and Slash", "Beat 'em up"),
    "Quiz/Trivia": ("Trivia",),
    "Pinball": ("Pinball",),
    "Adventure": ("Adventure",),
    "Indie": ("Indie",),
    "Arcade": ("Arcade",),
    "Visual Novel": ("Visual Novel",),
    "Card & Board Game": ("Card Game", "Board Game"),
    "MOBA": ("MOBA",),
    # themes
    "Action": ("Action",),
    "Fantasy": ("Fantasy",),
    "Science fiction": ("Sci-fi",),
    "Horror": ("Horror",),
    "Thriller": ("Thriller",),
    "Survival": ("Survival",),
    "Historical": ("Historical",),
    "Stealth": ("Stealth",),
    "Comedy": ("Comedy",),
    "Business": ("Management",),
    "Drama": (),
    "Non-fiction": (),
    "Sandbox": ("Sandbox",),
    "Educational": ("Education",),
    "Kids": ("Family Friendly",),
    "Open world": ("Open World",),
    "Warfare": ("War",),
    "Party": ("Party Game",),
    "4X (explore, expand, exploit, and exterminate)": ("4X", "Strategy"),
    "Erotic": ("Sexual Content",),
    "Mystery": ("Mystery",),
    "Romance": ("Romance",),
    # player perspectives
    "First person": ("First-Person",),
    "Third person": ("Third Person",),
    "Bird view / Isometric": ("Isometric",),
    "Side view": ("Side Scroller", "2D"),
    "Text": ("Text-Based",),
    "Auditory": (),
    "Virtual Reality": ("VR",),
    # game modes
    "Single player": ("Singleplayer",),
    "Multiplayer": ("Multiplayer",),
    "Co-operative": ("Co-op",),
    "Split screen": ("Split Screen", "Local Multiplayer"),
    "Massively Multiplayer Online (MMO)": ("Massively Multiplayer",),
    "Battle Royale": ("Battle Royale",),
}

# Two IGDB names together that Steam has one, more telling tag for: "Shooter" with "First person"
# is Steam's "FPS".
COMBOS: tuple[tuple[str, str, str], ...] = (
    ("Shooter", "First person", "FPS"),
    ("Shooter", "Third person", "Third-Person Shooter"),
    ("Shooter", "Bird view / Isometric", "Top-Down Shooter"),
    ("Platform", "Side view", "2D Platformer"),
    ("Platform", "Third person", "3D Platformer"),
    ("Fighting", "Side view", "2D Fighter"),
    ("Role-playing (RPG)", "Action", "Action RPG"),
    ("Role-playing (RPG)", "Hack and slash/Beat 'em up", "Action RPG"),
    ("Adventure", "Action", "Action-Adventure"),
    ("Horror", "Survival", "Survival Horror"),
)

# A Steam game's tags' votes as a share of its top tag's, by rank: the median over the 3,401 games
# with tags in the owner's database (2026-11-26). A mapped game's tags get these, so the taste
# reads them like a typical Steam game's.
TYPICAL_SHARES = (1.0, 0.924, 0.854, 0.799, 0.742, 0.692, 0.647, 0.604, 0.565, 0.529,
                  0.494, 0.463, 0.434, 0.413, 0.385, 0.357, 0.334, 0.3, 0.29, 0.27)
TOP_VOTES = 1000
MAX_TAGS = len(TYPICAL_SHARES)  # Steam shows a game's top 20 tags
MAX_KEYWORD_TAGS = 6  # Shenmue's keywords made "Artificial Intelligence" and "Dice" tags; keep the first few


class IgdbError(Exception):
    pass


class NotConfigured(IgdbError):
    pass


@dataclass
class IgdbGame:
    id: int
    name: str
    url: str | None = None
    released: int | None = None  # first release, Unix time
    cover_image: str | None = None
    platforms: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    perspectives: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    summary: str = ""
    storyline: str = ""
    rating: float | None = None  # 0-100, IGDB's users and critics together
    rating_count: int | None = None
    steam_id: int | None = None
    game_type: int | None = None

    @property
    def year(self) -> int | None:
        return datetime.fromtimestamp(self.released, timezone.utc).year if self.released else None

    @property
    def cover_url(self) -> str | None:
        return COVER_URL.format(self.cover_image) if self.cover_image else None

    @property
    def release_date(self) -> str | None:
        """In the form Steam's store writes it, "20 Nov, 2001", so the pages read it like any game's."""
        if not self.released:
            return None
        day = datetime.fromtimestamp(self.released, timezone.utc)
        return f"{day.day} {day:%b}, {day.year}"

    @property
    def text(self) -> str:
        """Summary and storyline: what likeness reads, as it reads a Steam game's store text."""
        return "\n\n".join(t for t in (self.summary.strip(), self.storyline.strip()) if t)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "url": self.url, "platforms": self.platforms, "genres": self.genres,
            "themes": self.themes, "perspectives": self.perspectives, "modes": self.modes,
            "keywords": self.keywords, "rating": self.rating, "rating_count": self.rating_count,
        }


def _names(items) -> list[str]:
    return [i["name"] for i in items or [] if isinstance(i, dict) and i.get("name")]


def parse(item: dict) -> IgdbGame:
    steam = None
    for ext in item.get("external_games") or []:
        if isinstance(ext, dict) and ext.get("external_game_source") == STEAM_SOURCE:
            try:
                steam = int(ext.get("uid"))
            except (TypeError, ValueError):
                continue
            if steam > 0:
                break
            steam = None
    rating = item.get("total_rating")
    return IgdbGame(
        id=int(item["id"]),
        name=" ".join(str(item.get("name") or "").split()) or f"IGDB game {item['id']}",
        url=item.get("url"),
        released=item.get("first_release_date"),
        cover_image=(item.get("cover") or {}).get("image_id"),
        platforms=_names(item.get("platforms")),
        genres=_names(item.get("genres")),
        themes=_names(item.get("themes")),
        perspectives=_names(item.get("player_perspectives")),
        modes=_names(item.get("game_modes")),
        keywords=_names(item.get("keywords")),
        summary=item.get("summary") or "",
        storyline=item.get("storyline") or "",
        rating=round(rating, 1) if isinstance(rating, (int, float)) else None,
        rating_count=item.get("total_rating_count"),
        steam_id=steam,
        game_type=item.get("game_type"),
    )


# ---- the API ----

def credentials() -> tuple[str, str] | None:
    cid, secret = os.getenv("IGDB_CLIENT_ID", "").strip(), os.getenv("IGDB_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def configured() -> bool:
    return credentials() is not None


_token: dict = {}  # {"client": id, "value": token, "until": time}; only in memory, a new one each run


def _access_token(cid: str, secret: str, timeout: float) -> str:
    if _token.get("client") == cid and _token.get("until", 0) > time.time():
        return _token["value"]
    resp = requests.post(
        TOKEN_URL, params={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}, timeout=timeout
    )
    if resp.status_code in (400, 401, 403):
        raise IgdbError("Twitch did not accept the IGDB client id and secret in .env.")
    resp.raise_for_status()
    data = resp.json()
    _token.update(client=cid, value=data["access_token"], until=time.time() + int(data.get("expires_in", 3600)) - 300)
    return _token["value"]


def query(endpoint: str, body: str, timeout: float = 20) -> list[dict]:
    creds = credentials()
    if creds is None:
        raise NotConfigured("IGDB isn't set up: add IGDB_CLIENT_ID and IGDB_CLIENT_SECRET to .env.")
    for attempt in (1, 2):
        token = _access_token(*creds, timeout)
        igdb_throttle.wait()
        resp = requests.post(
            f"{API_URL}/{endpoint}", data=body.encode("utf-8"), timeout=timeout,
            headers={"Client-ID": creds[0], "Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if resp.status_code == 401 and attempt == 1:  # the token ran out early: get a new one, once
            _token.clear()
            continue
        if resp.status_code == 429:
            raise RateLimited("IGDB asks us to slow down; try again in a moment.")
        resp.raise_for_status()
        return resp.json()
    raise IgdbError("IGDB refused the request.")


def _quote(text: str) -> str:
    return text.replace("\\", " ").replace('"', " ")


def search(name: str) -> list[IgdbGame]:
    """IGDB's games by that name, best match first; DLC, bundles, mods and editions left out.

    IGDB's own order put a 2020 Switch port above the 1996 Super Mario 64, and the original Crash
    Bandicoot third (checked 2026-11-26), so the answers are re-ranked: the exact name first, then
    names that start with it, each by how many people rated the game on IGDB (the well-known one first)."""
    name = " ".join(name.split())
    if not name:
        return []
    items = query(
        "games",
        f'search "{_quote(name)}"; fields {FIELDS}; '
        f"where game_type = ({','.join(str(t) for t in sorted(GAME_TYPES))}) & version_parent = null; "
        f"limit {SEARCH_POOL};",
    )
    return rank(name, [parse(i) for i in items])[:SEARCH_LIMIT]


def rank(name: str, games: list[IgdbGame]) -> list[IgdbGame]:
    wanted = _key(name)

    def order(pair: tuple[int, IgdbGame]) -> tuple:
        position, game = pair
        have = _key(game.name)
        closeness = 0 if have == wanted else 1 if have.startswith(wanted) else 2
        return (closeness, -(game.rating_count or 0), position)

    return [g for _, g in sorted(enumerate(games), key=order)]


def fetch(igdb_id: int) -> IgdbGame | None:
    items = query("games", f"fields {FIELDS}; where id = {int(igdb_id)};")
    return parse(items[0]) if items else None


# ---- tags ----

def _key(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").split())


def mapped_tags(game: IgdbGame, known_tags: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """The game's Steam tag names, most telling first: its genres; the combined tags two of its
    names make (COMBOS); keywords that are a known Steam tag's very name (IGDB's keywords are
    many and noisy: "psone classics", "boss fight"), at most MAX_KEYWORD_TAGS; then themes,
    perspectives and modes. At most MAX_TAGS.

    The order was measured (tools/check_igdb_tags.py, 2026-11-26): on 250 of the owner's Steam games
    IGDB also has, these tags came out at a median cosine of 0.42 to the games' real Steam tags
    (0.02 for a random other game); keywords matter most (0.31 without them), and putting the
    combined tags first, or the keywords last, did slightly worse."""
    known = {_key(t): t for t in known_tags}
    picked: list[str] = []

    def add(tag: str) -> None:
        if tag not in picked:
            picked.append(tag)

    names = set(game.genres + game.themes + game.perspectives)
    for name in game.genres:
        for tag in TAG_MAP.get(name, ()):
            add(tag)
    for one, two, tag in COMBOS:
        if one in names and two in names:
            add(tag)
    from_keywords = 0
    for word in game.keywords:
        if from_keywords < MAX_KEYWORD_TAGS and _key(word) in known and known[_key(word)] not in picked:
            add(known[_key(word)])
            from_keywords += 1
    for name in game.themes + game.perspectives + game.modes:
        for tag in TAG_MAP.get(name, ()):
            add(tag)
    return picked[:MAX_TAGS]


def votes_for(order: list[str]) -> dict[str, int]:
    """Tags in order of importance -> votes shaped like a typical Steam game's (TYPICAL_SHARES)."""
    seen: list[str] = []
    for tag in order:
        tag = " ".join(str(tag).split())
        if tag and tag not in seen:
            seen.append(tag)
    return {tag: round(TOP_VOTES * share) for tag, share in zip(seen, TYPICAL_SHARES)}


def ordered_tags(tags: dict[str, int] | None) -> list[str]:
    return [t for t, _ in sorted((tags or {}).items(), key=lambda kv: -kv[1])]


# ---- records ----

def game_id(igdb_id: int) -> int:
    return -int(igdb_id)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def known_tag_names(session: Session) -> set[str]:
    from sqlalchemy import select

    from shelflife.models import ON_STEAM
    names: set[str] = set()
    for tags in session.scalars(select(Game.tags).where(ON_STEAM, Game.type == "game")):
        names.update(tags or {})
    return names


SHORT_LENGTH = 300  # about what a Steam store page's short description runs to


def short(summary: str) -> str | None:
    """The summary's first paragraph, cut after its last whole sentence that fits."""
    first = " ".join((summary or "").strip().split("\n")[0].split())
    if len(first) <= SHORT_LENGTH:
        return first or None
    cut = first[:SHORT_LENGTH]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 0 else cut[: cut.rfind(" ")] + "…"


def apply(game: Game, found: IgdbGame, known_tags: set[str]) -> None:
    """Write what IGDB says into a non-Steam game's record. Tags you edited are kept."""
    game.name = found.name
    game.type = "game"
    game.igdb = found.as_dict()
    game.igdb_fetched_at = _now()
    game.about_text = found.text
    game.short_description = short(found.summary)
    game.release_date = found.release_date
    game.cover_url = found.cover_url
    game.genres = []
    if not game.tags_edited:
        game.tags = votes_for(mapped_tags(found, known_tags))
        game.tags_fetched_at = _now()


def upsert(session: Session, found: IgdbGame) -> Game:
    """The non-Steam game for an IGDB game, made or brought up to date (not committed)."""
    if found.steam_id:
        raise ValueError(f"{found.name} is on Steam: add it as a Steam game.")
    game = session.get(Game, game_id(found.id))
    if game is None:
        game = Game(steam_app_id=game_id(found.id), name=found.name, tags={}, genres=[])
        session.add(game)
    apply(game, found, known_tag_names(session))
    return game


def set_tags(session: Session, app_id: int, order: list[str]) -> Game:
    """Your own tags for a non-Steam game, most important first. They teach your taste from now on."""
    game = session.get(Game, app_id)
    if game is None or game.on_steam:
        raise ValueError("Only games that are not on Steam have tags you can change.")
    game.tags = votes_for(order)
    game.tags_edited = True
    game.tags_fetched_at = _now()  # reordering keeps the tags' length: this tells the kept taste it changed
    session.commit()
    return game


def reset_tags(session: Session, app_id: int) -> Game:
    """Back to the tags IGDB's data maps to (from what was stored, nothing is fetched)."""
    game = session.get(Game, app_id)
    if game is None or game.on_steam or not game.igdb:
        raise ValueError("Only games from IGDB can go back to IGDB's tags.")
    info = game.igdb
    found = IgdbGame(
        id=info["id"], name=game.name, genres=info.get("genres") or [], themes=info.get("themes") or [],
        perspectives=info.get("perspectives") or [], modes=info.get("modes") or [], keywords=info.get("keywords") or [],
    )
    game.tags = votes_for(mapped_tags(found, known_tag_names(session)))
    game.tags_edited = False
    game.tags_fetched_at = _now()
    session.commit()
    return game
