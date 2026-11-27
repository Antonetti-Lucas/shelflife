"""Prices as shown in the app, and keeping the ones on screen fresh.

A game's price (Game.price, store.parse_price) comes from Sync: once with the store details, then
step 8 for games you don't own. A page also asks for the prices it shows (/prices): those older
than SCREEN_PRICE_HOURS are read again, a hundred per store call.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCREEN_PRICE_HOURS = 6  # a price on screen older than this is read again

# How each currency is written: symbol, whether it goes after the amount, and the separators.
_STYLES = {
    "BRL": ("R$ ", False, ".", ","),
    "USD": ("$", False, ",", "."),
    "GBP": ("£", False, ",", "."),
    "EUR": ("€", True, ".", ","),
    "CAD": ("CA$ ", False, ",", "."),
    "AUD": ("A$ ", False, ",", "."),
    "JPY": ("¥", False, ",", "."),
}


def money(cents: int, currency: str) -> str:
    """12345, "BRL" -> "R$ 123,45"."""
    symbol, after, thousands, decimal = _STYLES.get(currency, (currency + " ", True, ",", "."))
    whole, part = divmod(abs(int(cents)), 100)
    digits = f"{whole:,}".replace(",", thousands)
    amount = digits if currency == "JPY" else f"{digits}{decimal}{part:02d}"
    amount = ("-" if cents < 0 else "") + amount
    return f"{amount} {symbol.strip()}" if after else f"{symbol}{amount}"


@dataclass(frozen=True)
class Shown:
    text: str  # "R$ 18,74", or "Free"
    was: str | None = None  # the full price, when on sale
    discount: int = 0  # percent off

    def as_dict(self) -> dict:
        return {"text": self.text, "was": self.was, "discount": self.discount}


def shown(price: dict | None) -> Shown | None:
    """How a stored price reads; None when there's none to show."""
    if not price:
        return None
    if price.get("free"):
        return Shown("Free")
    currency, final = price.get("currency"), price.get("final")
    if not currency or final is None:
        return None
    discount = int(price.get("discount") or 0)
    was = money(price["initial"], currency) if discount and price.get("initial") else None
    return Shown(money(final, currency), was, discount)


def final_cents(price: dict | None) -> int | None:
    """What a game costs now, in cents (0 when free), or None when no price is known: for sorting."""
    if not price:
        return None
    if price.get("free"):
        return 0
    return price.get("final") if price.get("currency") and price.get("final") is not None else None


def discount_percent(price: dict | None) -> int:
    """Percent off right now (0 when not on sale or no price is known)."""
    if not price or price.get("free"):
        return 0
    return int(price.get("discount") or 0)


def price_key(price: dict | None, name: str) -> tuple:
    """Cheapest first, free games first of all, games with no known price last."""
    cents = final_cents(price)
    return (cents is None, cents or 0, name.lower())


def discount_key(price: dict | None, name: str) -> tuple:
    """Biggest discount first, then the cheaper one; games not on sale last."""
    cents = final_cents(price)
    return (-discount_percent(price), cents is None, cents or 0, name.lower())


def owned(game) -> bool:
    """A game you own needs no price (one you play through Family Sharing still has one)."""
    return game.library_entry is not None and not game.family_shared


def stale(game, now: datetime | None = None) -> bool:
    fetched = game.price_fetched_at
    if fetched is None:
        return True
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if fetched.tzinfo is not None:
        fetched = fetched.astimezone(timezone.utc).replace(tzinfo=None)
    return now - fetched > timedelta(hours=SCREEN_PRICE_HOURS)


@dataclass(frozen=True)
class Low:
    """A game's lowest price ever on Steam (Game.price_low, from IsThereAnyDeal), as it reads."""
    text: str  # "R$ 12,49"
    cut: int  # percent off then; 0: never on sale on Steam
    when: str | None  # "Mar 2025"
    now: bool  # on sale today at that low (or lower)
    record: bool = False  # ...and it was never this cheap before this sale
    before: str | None = None  # the low it beat, "R$ 19,99, Nov 2024", when record
    two_years: bool = False  # on sale at its lowest in two years, though not its lowest ever


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RECORD_DAYS = 14  # Steam sales last up to two weeks: a record seen longer ago belongs to an older sale


def _month(date: str | None) -> str | None:
    date = date or ""
    if len(date) >= 7 and date[5:7].isdigit() and 1 <= int(date[5:7]) <= 12:
        return f"{_MONTHS[int(date[5:7]) - 1]} {date[:4]}"
    return None


def low_of(game, today=None) -> Low | None:
    """None when no low is known or it is in another currency than the store price (a STEAM_COUNTRY
    change: the two can't be compared until the next sync reads it again).

    A sale is a record (never this cheap before) when it is below the stored low (a sync has not
    read the new record yet), or at a low that replaced an older one (itad.keep_before) during
    the last RECORD_DAYS. Otherwise a sale at the low only matches it."""
    low, price = game.price_low, game.price
    if not low or low.get("cents") is None or not price or price.get("free"):
        return None
    if price.get("currency") and price["currency"] != low.get("currency"):
        return None
    final = price.get("final")
    now = bool(price.get("discount")) and final is not None and final <= low["cents"]
    record, before = False, None
    if now:
        today = today or datetime.now(timezone.utc).date()
        seen = low.get("seen")
        fresh = bool(seen) and (today - datetime.fromisoformat(seen).date()).days <= RECORD_DAYS
        if final < low["cents"]:
            record, beaten = True, {"cents": low["cents"], "date": low.get("date")}
        elif fresh and low.get("before"):
            record, beaten = True, low["before"]
        if record:
            when = _month(beaten.get("date"))
            before = money(beaten["cents"], low["currency"]) + (f", {when}" if when else "")
    two_years = (
        not now and bool(price.get("discount")) and final is not None
        and low.get("y2") is not None and final <= low["y2"]
    )
    return Low(money(low["cents"], low["currency"]), int(low.get("cut") or 0), _month(low.get("date")), now, record, before, two_years)
