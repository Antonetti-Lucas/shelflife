"""What Wikidata knows about a Steam game: the series it belongs to and its first release date.

Wikidata's data is CC0 (public domain). Its public query service (query.wikidata.org) asks every client
for a User-Agent that names the tool and says how to reach its maker, allows 60 seconds of query time a
minute and five parallel queries, and answers 429 with Retry-After beyond that. So: one query at a time,
at most BATCH games a query, a pause between queries, and a stop on 429. Only Steam app ids are sent.

A game is found by its Steam application ID (property P1733); its series is P179 (with the number in the
series, P1545 and its first release date P577; a game released on several platforms has several dates,
the earliest is kept).
"""

import re
from dataclasses import dataclass, field

import requests

from shelflife.throttle import RateLimited, Throttle

URL = "https://query.wikidata.org/sparql"
# The policy asks for the tool's name and a way to reach its maker: the project's public repository.
HEADERS = {
    "User-Agent": "ShelfLife/1.0 (https://github.com/Antonetti-Lucas/shelflife; personal game diary)",
    "Accept": "application/sparql-results+json",
}
BATCH = 150  # Steam ids a query: about 6 seconds, well inside the 60 a minute
LABEL_LANGUAGES = "en,mul,es,pt,fr,de,it,ja,ru"  # a series with no English name gets one in another language
wikidata_throttle = Throttle(3.0)

QUERY = """
SELECT ?steam ?item ?series ?seriesLabel ?ordinal ?pub WHERE {
  VALUES ?steam { %s }
  ?item wdt:P1733 ?steam .
  OPTIONAL { ?item p:P179 ?s . ?s ps:P179 ?series . OPTIONAL { ?s pq:P1545 ?ordinal } }
  OPTIONAL { ?item wdt:P577 ?pub }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "%s" . }
}
"""
_BARE_ID = re.compile(r"^Q\d+$")  # what the label service returns when an entity has no name in any language


@dataclass
class WikidataGame:
    """What was found for one Steam game."""

    item: str  # its Wikidata id, "Q74539"
    series: list[dict] = field(default_factory=list)  # [{"id": "Q19308", "label": "Hitman", "ordinal": "3"}]
    released: str | None = None  # the earliest release date, "2012-11-20"

    def as_dict(self) -> dict:
        return {"item": self.item, "series": self.series, "released": self.released}


def _last(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def parse(bindings: list[dict]) -> dict[int, WikidataGame]:
    """Steam app id -> what was found, from the query's rows (one row per series and release date)."""
    found: dict[int, WikidataGame] = {}
    for row in bindings:
        try:
            app_id = int(row["steam"]["value"])
        except (KeyError, ValueError):
            continue
        game = found.setdefault(app_id, WikidataGame(_last(row["item"]["value"])))
        if "series" in row:
            series_id = _last(row["series"]["value"])
            label = row.get("seriesLabel", {}).get("value", "")
            ordinal = row.get("ordinal", {}).get("value")
            known = next((s for s in game.series if s["id"] == series_id), None)
            if known is None:
                known = {"id": series_id, "label": label, "ordinal": ordinal}
                game.series.append(known)
            elif not known["ordinal"] and ordinal:
                known["ordinal"] = ordinal
        if "pub" in row:
            day = row["pub"]["value"][:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and (game.released is None or day < game.released):
                game.released = day
    for game in found.values():  # a series with no name anywhere cannot be shown
        game.series = [s for s in game.series if s["label"] and not _BARE_ID.match(s["label"])]
    return found


def fetch_games(app_ids: list[int], timeout: float = 90) -> dict[int, WikidataGame]:
    """Ask Wikidata about up to BATCH Steam games. A game it has no item for is left out of the answer."""
    if len(app_ids) > BATCH:
        raise ValueError(f"Ask for at most {BATCH} games at a time")
    wikidata_throttle.wait()
    values = " ".join(f'"{int(a)}"' for a in app_ids)
    resp = requests.get(
        URL, params={"query": QUERY % (values, LABEL_LANGUAGES), "format": "json"}, headers=HEADERS, timeout=timeout
    )
    if resp.status_code in (429, 503):
        wait = resp.headers.get("Retry-After")
        raise RateLimited(f"Wikidata asks us to slow down{f' (try again in {wait} seconds)' if wait else ''}.")
    resp.raise_for_status()
    return parse(resp.json().get("results", {}).get("bindings") or [])
