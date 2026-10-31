"""Database models (see "Data model" in the README)."""

from datetime import date, datetime

from sqlalchemy import JSON, Date, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shelflife.db import Base

OFF_TOPIC = "off-topic"  # Game.type of a game a search set aside as not really about its tag


class Setting(Base):
    """A choice you made in the app, kept between runs (see shelflife/settings.py)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class Game(Base):
    __tablename__ = "games"

    steam_app_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    # Steam store type: "game", "dlc", "demo", "software", ... or "unavailable"
    # when the store has no page for the app (delisted, region-locked), or
    # "off-topic" for a game a search looked at and found not really about the
    # tag it was listed under (kept, with its tags, so it is not looked up again).
    type: Mapped[str | None]
    short_description: Mapped[str | None]
    # The store page's "About this game" text, cleaned of markup. "" when the store has none;
    # None when it was never read. It is long, so a query loads it only when asked to.
    about_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    genres: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Community tags -> votes, e.g. {"Roguelike": 5400, "Cozy": 120}
    tags: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    release_date: Mapped[str | None]  # kept as Steam's text; formats vary
    cover_url: Mapped[str | None]
    details_fetched_at: Mapped[datetime | None]
    tags_fetched_at: Mapped[datetime | None]

    # Steam user review counts (all languages); drives the quality filter.
    reviews_positive: Mapped[int | None]
    reviews_negative: Mapped[int | None]
    reviews_fetched_at: Mapped[datetime | None]

    # True for games you can play through Steam Family Sharing but don't own.
    # Their library entry has no playtime (the Web API can't see it).
    family_shared: Mapped[bool | None]

    # None for games you don't own (the discovery pool).
    library_entry: Mapped["LibraryEntry | None"] = relationship(
        back_populates="game", uselist=False
    )


class WishlistItem(Base):
    """A game on your Steam wishlist as of the last sync (see shelflife/wishlist.py)."""

    __tablename__ = "wishlist_items"

    steam_app_id: Mapped[int] = mapped_column(ForeignKey("games.steam_app_id"), primary_key=True)
    priority: Mapped[int | None]  # your own rank, 1 at the top; 0 or None: never ranked
    added_at: Mapped[datetime | None]  # when you wishlisted it
    seen_at: Mapped[datetime]  # the last sync that found it on the list


class PlaytimeSnapshot(Base):
    """Total playtime as of a sync. Steam only reports lifetime totals, so these
    snapshots, recorded at every sync from now on, are the only way to know how
    much was played in a given period."""

    __tablename__ = "playtime_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    steam_app_id: Mapped[int] = mapped_column(ForeignKey("games.steam_app_id"), index=True)
    taken_at: Mapped[datetime]
    playtime_total_min: Mapped[int]


class DiaryEntry(Base):
    """Your own verdict on a game: rating, review, "not interested", and where you
    are with it (status, dates, favorite)."""

    __tablename__ = "diary_entries"

    steam_app_id: Mapped[int] = mapped_column(
        ForeignKey("games.steam_app_id"), primary_key=True
    )
    rating: Mapped[float | None]  # 0.5-5 in half steps
    not_interested: Mapped[bool] = mapped_column(default=False)
    review: Mapped[str | None]
    updated_at: Mapped[datetime]

    # "playing", "played" or "dropped"; None = not tracked. The dates are optional
    # (an old game you can't date stays blank). Existing databases get these
    # columns as NULL, so favorite is None, not False, for older rows.
    status: Mapped[str | None]
    started_on: Mapped[date | None] = mapped_column(Date)
    finished_on: Mapped[date | None] = mapped_column(Date)
    favorite: Mapped[bool | None]


class LibraryEntry(Base):
    __tablename__ = "library_entries"

    steam_app_id: Mapped[int] = mapped_column(
        ForeignKey("games.steam_app_id"), primary_key=True
    )
    playtime_total_min: Mapped[int] = mapped_column(default=0)
    playtime_recent_min: Mapped[int] = mapped_column(default=0)
    last_played: Mapped[datetime | None]
    last_synced: Mapped[datetime]

    # Achievement progress (Step 3). total is None until fetched, 0 = the game
    # has no achievements.
    achievements_total: Mapped[int | None]
    achievements_unlocked: Mapped[int | None]
    # Lowest global unlock rate (%) among the achievements you hold.
    rarest_unlocked_pct: Mapped[float | None]
    first_unlock_at: Mapped[datetime | None]
    last_unlock_at: Mapped[datetime | None]
    # Every unlock time (unix seconds): the best evidence of WHEN you really
    # played, since Steam keeps no playtime history. None = not fetched/no unlocks.
    unlock_times: Mapped[list[int] | None] = mapped_column(JSON(none_as_null=True))
    achievements_fetched_at: Mapped[datetime | None]

    game: Mapped[Game] = relationship(back_populates="library_entry")

    @property
    def playtime_total_hours(self) -> float:
        return self.playtime_total_min / 60
