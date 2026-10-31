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
- **Web app:** recommendations, a library you can rate and edit in place, an Explore page, a diary, stats, game pages, and your taste profile.

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

The fetching scripts are deliberately slow to respect the services' limits (about 3 seconds per game, so 300 games take roughly 15 minutes). They save progress as they go: stop with Ctrl-C and run again to continue. Re-run `sync` and `achievements` any time to refresh your playtime.

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
| `python -m scripts.discover [--per-tag N]` | Grow the pool of games you don't own |
| `python -m scripts.feedback not-interested "Name"` | Dismiss a game (also `undo`, `list`) |
| `python -m scripts.import_family [file]` | Import a Family Sharing export (below) |
| `python -m scripts.hours [--template]` | Enter family-game hours from a file |
| `python -m scripts.import_diary` | One-time import of old `ratings.csv` / `feedback.csv` |

## Web app

`python -m scripts.web`, then open <http://127.0.0.1:8000>. It listens only on your own machine and has no login, so don't expose it to a network.

- **Recommendations:** your backlog, family and discovery lists, paged, each pick with the games and tags behind it. Rate or dismiss from the card.
- **Library:** your owned and family games with covers. Set a rating or a status, or type hours for family games, right in the row. Filter by status, or use the **Recommended** filter to keep the unplayed games that fit you best (top 50%, 25% or 10%).
- **Explore:** games you don't own, ranked by fit, with the same filter, search and sorting.
- **Diary:** your games by status (playing, played, dropped), newest first, with your favorites on top. Set a status, its dates and a favorite on a game's page; picking a status fills in today's date, and you can clear it for a game you can't date. "Played" means finished or played as much as you wanted, so it fits endless multiplayer games too.
- **Stats:** games and hours in your library, your diary by status, games finished per year, how you rate, and your most played games.
- **Game page, My ratings, My taste:** why a game fits you and how you played it; your ratings and reviews (edit or delete in place); and the tag profile behind it all.

## Family Sharing (optional)

Steam's Web API only lists games on your own account. ShelfLife imports Family Sharing games the way SteamDB's browser extension does, with a script **you run yourself**, so your login token never touches ShelfLife:

1. Log in at <https://store.steampowered.com>, open DevTools (F12) > Console, paste [`tools/export_family_library.js`](tools/export_family_library.js) and press Enter. It downloads `family-library.json`.
2. Put that file in a `data/` folder (create it) and run `python -m scripts.import_family`.
3. Run `python -m scripts.sync` and `python -m scripts.achievements` again.

The script gets a short-lived token from the Steam store using your own cookies and calls `IFamilyGroupsService/GetSharedLibraryApps`. The token stays in the tab's memory and is never printed, saved or sent anywhere. These endpoints are undocumented by Valve and may change. The export lists which games your family members own, so keep it private.

**Hours.** Steam reports no playtime for family games, so enter what your Steam client shows: in the hours box on the game's page or in the Library, or in bulk with `python -m scripts.hours --template` (fill in `data/hours.csv`, then run `python -m scripts.hours`). Hours only apply to family games.

## Data and terms

Data comes from the Steam Web API (library, playtime, achievements), the Steam store (details, covers), Valve's `IUserReviewsService` (review counts), and SteamSpy (community tags, 1 request/second). ShelfLife follows the [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms): your key stays in `.env` on your machine, data is fetched only when you run a command, everything is stored locally, and it is not affiliated with or endorsed by Valve. A few endpoints used (Steam store `appdetails`, the family export) are undocumented and may change.

Personal data lives in `shelflife.db`, `.env` and the `data/` folder, all git-ignored.

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

**2. Taste profile.** Each played game gets a weight from −1 to +1: a rating gives `(rating − 3) / 2` (and is never faded), and "not interested" counts as 2 stars. A favorite counts as 5 stars until you rate the game. A status (playing, played, dropped) is a record only: it takes the game out of the recommendations but does not change your taste, because "dropped" can mean a story game quit halfway or an endless multiplayer game you left after hundreds of hours. An unrated game gets up to +0.5 from its engagement score. The profile sums each game's tags (vote share of the top 15 tags, scaled down for tags common in your library) times its weight.

**3. Scoring.**

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
