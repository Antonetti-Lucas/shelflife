"""What the list pages (Library, Explore, Wishlist, Diary, Ratings) share: reading the page number,
cutting a list into pages, the search box and tag filter, and the "recommended" cut."""

from dataclasses import dataclass, field

from shelflife.models import Game
from shelflife.tagfilter import TagIndex, matches, parse_match, parse_tags

REC_FILTERS = {"all": None, "50": 50, "25": 25, "10": 10}  # "recommended": top N% by fit


def whole(raw: str | int | None, default: int) -> int:
    """A page number or size from a link; anything that isn't a plain whole number (a typo, an
    old bookmark, "abc") gives the default instead of an error page."""
    text = str(raw if raw is not None else "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 6 else default


@dataclass
class Page:
    shown: list  # the items on this page
    page: int
    pages: int
    total: int  # items on all pages


def paginate(items: list, page: str | int | None, size: int) -> Page:
    """One page of `items`; a page number past either end gives the nearest page."""
    pages = max(1, -(-len(items) // size))
    page = max(1, min(whole(page, 1), pages))
    return Page(items[(page - 1) * size : page * size], page, pages, len(items))


@dataclass
class ListFilter:
    """The search box and tag filter a list page has, as typed (`q`, `tag`) and as understood."""

    q: str
    tag: str
    match: str  # "all" or "any" of the tags
    wanted: list[str] = field(default_factory=list)
    needle: str = ""

    @classmethod
    def read(cls, q: str, tag: str, match: str) -> "ListFilter":
        return cls(q, tag, parse_match(match), parse_tags(tag), q.strip().lower())

    @property
    def active(self) -> bool:
        return bool(self.needle or self.wanted)

    def keeps(self, game: Game) -> bool:
        return (not self.needle or self.needle in game.name.lower()) and matches(game.tags, self.wanted, self.match)

    def context(self, tag_index: TagIndex) -> dict:
        """What the search box and tag box need to show it again. `tag_index` holds the tags of
        the whole list, before this filter."""
        return {
            "q": self.q,
            "tag": self.tag,
            "match": self.match,
            "tag_options": tag_index.popular(),
            "unknown_tags": tag_index.unknown(self.wanted),
        }


def top_share(scores: dict[int, float], percent: int | None) -> set[int] | None:
    """Ids in the top `percent`% by score; None means no filter."""
    if percent is None:
        return None
    ranked = sorted(scores, key=scores.get, reverse=True)
    keep = max(1, -(-len(ranked) * percent // 100)) if ranked else 0
    return set(ranked[:keep])


def recommended(rec: str, scores: dict[int, float]) -> tuple[str, set[int] | None]:
    """The "recommended" choice as understood ("all" for anything unknown), and the ids it keeps
    (None: all of them)."""
    return (rec if rec in REC_FILTERS else "all"), top_share(scores, REC_FILTERS.get(rec))
