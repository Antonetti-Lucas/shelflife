# ShelfLife

[![Tests](https://github.com/Antonetti-Lucas/shelflife/actions/workflows/tests.yml/badge.svg)](https://github.com/Antonetti-Lucas/shelflife/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> A Letterboxd-style game diary that imports your Steam library, uses your playtime and achievements to understand how you *really* play, and recommends what to play next.

**Status:** early development. It runs entirely on your own computer: nothing is hosted, and your data never leaves your machine.

Steam knows what you own and how long you've played, but not what you *thought* of a game, and it can't tell "I loved this 6-hour game" from "I gave up on this 150-hour RPG." ShelfLife combines your ratings, your playtime, and your achievements (and when you unlocked them) into a taste profile, then recommends games from your backlog, your Steam Family library, and games you don't own yet.

## Features

- **Steam import:** owned games, playtime, store details, community tags, review counts and achievements.
- **Family Sharing import** (optional): games you can play through a Steam Family.
- **Diary:** 0.5–5 star ratings, reviews, "not interested" marks, a status (playing, played, dropped) with dates, and favorites, stored locally.
- **Recommendations:** three lists (backlog, family, games you don't own), each with a reason.
- **Web app:** recommendations, a library you can rate and edit in place, an Explore page with a button to find more games, similar games for any game, a diary, stats, game pages, your taste profile, and a Sync button.

## Screenshots

Taken on demo data: popular Steam games with made-up playtime, ratings and reviews for an imaginary player.

**Recommendations**, in three lists, each pick with the games and tags behind it:

![Recommendations](docs/screenshots/recommendations.jpg)

**Library**, with ratings and hours editable in the row, and **Explore** for games you don't own:

![Library](docs/screenshots/library.jpg)

![Explore](docs/screenshots/explore.jpg)

**Game page**, showing why a game fits you and how you played it, and **My ratings**:

![Game page](docs/screenshots/game-page.jpg)

![My ratings](docs/screenshots/my-ratings.jpg)

## Setup

You need Python 3.11 or newer, a Steam account, and a free Steam Web API key. (Developed on Python 3.15; the test workflow runs 3.11–3.13.)

**1. Install**

```bash
git clone <this repository>
cd <the repository folder>
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

**2. Get your Steam credentials**

- **API key:** create one at <https://steamcommunity.com/dev/apikey> (any domain name works, e.g. `localhost`). Keep it private.
- **SteamID:** your SteamID64, a 17-digit number (`XXXXXXXXXXXXXXXXX`). If your profile address has a number after `/profiles/`, that's it; otherwise paste your profile address into <https://steamid.io>.
- **Privacy:** in your Steam profile's *Privacy Settings*, set **Game details** to **Public**.

**3. Configure**

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored; never share it.

```
STEAM_API_KEY=your_key_here
STEAM_ID=your_64_bit_steam_id_here
```

**4. Run**

```bash
python -m scripts.top_games      # quick check that your key works
python -m scripts.sync           # library, store details and tags
python -m scripts.achievements   # achievements and when you unlocked them
python -m scripts.discover       # a pool of well-reviewed games you don't own
python -m scripts.web            # then open http://127.0.0.1:8000
```

You can also sync from the web app: its **Sync** page runs the same steps (library, achievements, store details, review counts) in the background with a progress bar and a Stop button, re-reads what could have changed, and carries on where it left off if you stop it. The fetching scripts are deliberately slow to respect the services' limits (about 3 seconds per game, so 300 games take roughly 15 minutes). They save progress as they go: stop with Ctrl-C and run again to continue. Re-run `sync` and `achievements` any time to refresh your playtime.

**If something goes wrong**

- *"No games returned"*: *Game details* isn't Public, or `STEAM_ID` isn't the 17-digit ID.
- *"Steam rejected the API key"*: check `STEAM_API_KEY`.
- *"rate limiting"*: wait a few minutes and run the script again.
- *Start over*: delete `shelflife.db` (your ratings live in it) and run step 4 again.

### Other commands

| Command | What it does |
|---|---|
| `python -m scripts.sync [--limit N] [--fill-tags]` | Import the library; fetch details and tags |
| `python -m scripts.scores [N]` | Rank your played games by engagement score |
| `python -m scripts.recommend [N]` | Print recommendations in the terminal |
| `python -m scripts.discover [--per-tag N] [--tags "A,B"]` | Find new games you don't own (the same as Explore's button) |
| `python -m scripts.feedback not-interested "Name"` | Dismiss a game (also `undo`, `list`) |
| `python -m scripts.import_family [file]` | Import a Family Sharing export (below) |
| `python -m scripts.hours [--template]` | Enter family-game hours from a file |
| `python -m scripts.import_diary` | One-time import of old `ratings.csv` / `feedback.csv` |

## Web app

`python -m scripts.web`, then open <http://127.0.0.1:8000>. It listens only on your own machine and has no login, so don't expose it to a network.

- **Recommendations:** your backlog, family and discovery lists, paged, each pick with the games and tags behind it. Rate or dismiss from the card.
- **Library:** your owned and family games with covers. Set a rating or a status, or type hours for family games, right in the row. Search by name, filter by tags or status, sort by most played or **recently played**, or use the **Recommended** filter to keep the unplayed games that fit you best (top 50%, 25% or 10%).
- **Explore:** games you don't own, ranked by fit, with the same filter, search, tags and sorting. **Find more games** adds new ones for the genres or tags you pick (or the tags you like most), or with no tags at all (the best-reviewed games from SteamSpy's list of popular games), for a number of games you choose, and shows its progress. Every game page has **Games like this** (Explore ranked by how close games are to that one) and a button to search Steam for new games like it.
- **Diary:** one tab each for playing, played, dropped and your favorites, newest first, paged, with search and a tag filter. Set a status, optional dates and a favorite on a game's page; dates are never filled in for you. "Played" means finished or played as much as you wanted, so it fits endless multiplayer games too.
- **Tags:** pick several tags in the Library, Explore or Diary, and choose whether a game needs all of them or any of them. Tags match whole and ignore case, a game "has" a tag if it is among its 20 most voted, and the box suggests the tags that exist.
- **Sync:** one button for your library, achievements, store details and review counts, with progress and a Stop button. A sync re-reads what could have changed: achievements for games you played since they were last read, or not read for 30 days (developers add achievements); store details for new games, games that came back empty a week ago, and the 30 oldest when they are over 90 days old; and review counts for every game that has none or whose counts are a month old. **Check everything again** ignores those limits and re-reads every game (slow, and the page says how long). It saves as it goes, so a stopped sync resumes, it needs `STEAM_API_KEY` and `STEAM_ID` in `.env`, and it never runs at the same time as a game search.
- **Stats:** games and hours in your library, your diary by status, games finished per year, how you rate, and your most played games.
- **Game page, My ratings, My taste:** why a game fits you and how you played it; your ratings and reviews (edit or delete in place); and the tag profile behind it all, where you also choose whether your taste learns from your ratings, from how you play, or from both.

## Family Sharing (optional)

Steam's Web API only lists games on your own account. ShelfLife imports Family Sharing games the way SteamDB's browser extension does, with a script **you run yourself**, so your login token never touches ShelfLife:

1. Log in at <https://store.steampowered.com>, open DevTools (F12) > Console, paste [`tools/export_family_library.js`](tools/export_family_library.js) and press Enter. It downloads `family-library.json`.
2. Put that file in a `data/` folder (create it) and run `python -m scripts.import_family`.
3. Run `python -m scripts.sync` and `python -m scripts.achievements` again.

The script gets a short-lived token from the Steam store using your own cookies and calls `IFamilyGroupsService/GetSharedLibraryApps`. The token stays in the tab's memory and is never printed, saved or sent anywhere. These endpoints are undocumented by Valve and may change. The export lists which games your family members own, so keep it private.

**Hours.** Steam reports no playtime for family games, so enter what your Steam client shows: in the hours box on the game's page or in the Library, or in bulk with `python -m scripts.hours --template` (fill in `data/hours.csv`, then run `python -m scripts.hours`). Hours only apply to family games.

## Data and terms

Data comes from the Steam Web API (library, playtime, achievements), the Steam store (details, covers), Valve's `IUserReviewsService` (review counts), and SteamSpy (community tags, 1 request/second). ShelfLife follows the [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms): your key stays in `.env` on your machine, data is fetched only when you run a command or press a button, everything is stored locally, and it is not affiliated with or endorsed by Valve. A few endpoints used (Steam store `appdetails`, the family export) are undocumented and may change.

Personal data lives in `shelflife.db`, `.env` and the `data/` folder, all git-ignored.

## Privacy

ShelfLife runs entirely on your own computer. It has no server, no account, no analytics and no tracking, and your diary never leaves your machine.

**What it stores, and where**

- `.env`: your Steam API key and SteamID.
- `shelflife.db`: your library (games, hours, when you last played, achievement counts and when you unlocked them), the hours you type in for family games, which games are shared with you through Family Sharing, your diary (ratings, reviews, statuses and dates, favorites, "not interested"), your settings, and public store data for each game (names, descriptions, tags, review counts, cover addresses).
- `data/`: files you create or import, such as the Family Sharing export (it lists which games your family members own, so keep it private) and old CSV files.

Delete `.env`, `shelflife.db` and `data/` and nothing of yours is left.

**What it sends, and to whom** (only when you run a command or press a button)

- **Valve's Steam Web API** (`api.steampowered.com`): your API key and SteamID, to read your own library and achievements, and your key to look up game names. Review counts and global achievement rates are asked for by game id alone.
- **The Steam store** (`store.steampowered.com`) and **SteamSpy** (`steamspy.com`): game ids and tag names only. They never receive your key, SteamID, ratings or diary.
- **Your browser** loads cover images from Steam's servers when you open a page.
- Nothing else: no outside fonts, scripts or analytics. The web app listens only on `127.0.0.1` and has no login, so don't expose it to a network.

**Steam's terms**

ShelfLife is a personal tool. Each person uses their own API key (it is personal to you, so don't share it or commit it) to read their own account, within Valve's limit of 100,000 calls a day (a full sync uses a few hundred), and it is not affiliated with or endorsed by Valve. The [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms) ask applications to say what Steam data they store; the list above is that statement.

Most of what ShelfLife reads comes from Valve's documented Web API. A few things come from endpoints Valve doesn't document: the store's `appdetails` endpoint, the review counts, the tag votes read from a game's store page when SteamSpy has none, and the Family Sharing export. They are read-only lookups, throttled and run only when you ask (the family export reads only your own account). Because they are undocumented they may change or stop working, and the [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/) has a general rule about bots and scripts on Steam, whose examples are about cheating and account abuse. Using them is your choice; only the Family Sharing import is optional.

Game names, store data and cover art belong to Valve and the game publishers. The licence below covers ShelfLife's code only.

## How recommendations work

**1. Engagement score.** For each game you've played, a sum of points (weights in `shelflife/score.py` and `shelflife/recency.py`):

| Signal | Points |
|---|---|
| Playtime | `log10(1 + hours)`: 10h = 1, 100h = 2, 1000h = 3 |
| Achievements | up to 2 × share unlocked (games with 10+ achievements) |
| Rare achievement | +1 if you hold one under 5% of players have, +0.5 if under 10% |
| Still playing | +0.5 if played in the last two weeks |
| Recency | +0.5 (all play is current) to −0.5 (ancient); 0 with no date evidence |

*Recency:* Steam gives only lifetime hours and a last-played date, and one minute today resets that date. So the unlock dates of your achievements show *when* you really played: each unlock counts less the older it is (halving every 3 years). A game you play right now counts as fully current.

**2. Taste profile.** What the profile learns from is your choice, on the My taste page: *Mix of both* (the default), *Your ratings only* or *How you play only*. In the mix, a game you rated counts 70% by its rating, `(rating − 3) / 2`, from −1 to +1, and 30% by how you played it (the engagement score scaled from 0 to 1, so play lifts a rating and never lowers it); a game you did not rate counts by how you played it, up to +0.5. "Not interested" counts as 2 stars and a favorite as 5 until you rate the game. In *ratings only* a game you did not rate teaches nothing; in *how you play only* ratings, favorites and "not interested" are ignored for taste ("not interested" still hides the game). A status (playing, played, dropped) is a record only: it takes the game out of the recommendations but does not change your taste, because "dropped" can mean a story game quit halfway or an endless multiplayer game you left after hundreds of hours. The profile sums each game's tags times its weight. A tag counts by how relevant it is to the game: its votes relative to the game's top tag, squared (a tag with half the top tag's votes counts a quarter as much), for the top 15 tags, scaled down for tags common in your library. The squaring is one setting, `RELEVANCE_POWER` in `shelflife/tags.py`.

**3. Finding new games.** Explore's **Find more games** button (and `python -m scripts.discover`) works like this: SteamSpy lists every game under a tag, with its review counts. That list is loose (any game with even a few votes for the tag is in it), so games you already have and games with under 300 reviews are dropped, the rest are ranked by the lower bound of their positive-review share (more reviews rank higher at equal praise), and then, going down the ranking with the tags taking turns, each game's own tag votes are read and the game is kept only if the searched tag is among its 10 most voted. The others are remembered, with their tags, so they are not fetched again. Kept games get their store details fetched, about 5 seconds a game in all, and then take part in the rankings. For "games like this one", the tags searched are that game's most distinctive ones and a game must also be at least 15% similar to it. SteamSpy matches tag names in a particular letter case (and a few under older names), so several spellings are tried. Without tags, the search reads SteamSpy's popularity-ordered list instead (up to 3 pages of 1,000 games, a minute apart as SteamSpy asks, and only as many as needed) and keeps the best-reviewed games you don't have, with no tag check; raise "With at least" to 1,000 reviews to favour better-known games.

**4. Scoring.**

```
score = cosine_similarity(game_tags, taste_profile) × quality
quality = (Wilson lower bound of the positive-review share) ^ 2
```

The Wilson bound is cautious with few reviews: about 0.9 at 95% positive, 0.5 at 70%, 0.36 with no review data. Candidates are backlog games (owned, under an hour played), family games you haven't played, and games you don't own (300+ reviews). Other editions of games you already have are skipped.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests use a temporary database and folder (never your own data) and make no network calls. Stack: Python, FastAPI with Jinja templates, SQLite through SQLAlchemy, pytest, GitHub Actions. The data model is in `shelflife/models.py`.

To regenerate the screenshots, `python tools/make_demo_db.py demo.db` builds the demo database (real games fetched from Steam, made-up play history), and `DATABASE_URL=sqlite:///demo.db python -m scripts.web --port 8001` runs the app on it.

## License

[MIT](LICENSE). This covers the code only; data from Steam and other services stays under their own terms.
