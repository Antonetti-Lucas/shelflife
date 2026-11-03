# Using ShelfLife

The [README](../README.md) gets you running. This page covers each page of the web app, what Sync reads, the command-line scripts, Family Sharing, and what to do when something goes wrong.

## The pages

- **For you:** your backlog, family, wishlist and discovery lists, each pick with the games and tags behind it. Rate or dismiss from the card; **See all** opens one list, paged.
- **Library:** your owned and family games, and games you played on other platforms, with covers. Set a rating or a status, or type hours for family games, right in the row. Search by name, filter by tags, status or length, click a column's name to sort by it (again to turn the order round), or use the **Recommended** filter to keep the unplayed games that fit you best (top 50%, 25% or 10%).
- **Explore:** games you don't own, ranked by fit, with the same filters and sorting. Each card shows its price in your currency, refreshed when you open the page. **Find more games** adds new ones for the genres or tags you pick (or the tags you like most), or with no tags at all (the best-reviewed popular games), for a number of games you choose, and shows its progress.
- **Wishlist:** your Steam wishlist ranked by how well each game fits your taste, with your own rank and the date you added each one. It is read by Sync (the wishlist must be public on Steam). Each wishlisted game also lifts games very like it a little (see [how it works](how-it-works.md#2-what-your-taste-learns-from)). To wishlist a game you found in ShelfLife, use **Wishlist ↗** on its card or **Wishlist it on Steam** on its page: they open the Steam store page, where you click "Add to your wishlist", and your next sync brings it in. (ShelfLife never changes your Steam account itself.)
- **Diary:** one tab each for playing, played, dropped, your favorites and the games you marked "not interested" (with Undo), newest first, with search and a tag filter. Set a status, optional dates and a favorite on a game's page; dates are never filled in for you. "Played" means finished or played as much as you wanted, so it fits endless multiplayer games too.
- **My ratings:** a wall of portrait covers with your stars (click them to rate), favorites and reviews marked, filtered by rating, decade and tags and sorted by rating, date rated, release date or name. Switch to the list view to edit or delete ratings and reviews.
- **Lists:** lists of games you make and name ("Best of 2024", "Cozy evenings"), with an optional description. Each list can be **Ranked** (numbered 1, 2, 3…) or not, and can show **My ratings** (your stars under each game, which you can click to rate) or not; both are one click at the top of the list. Add games with the search on the list's page or with **Add to list** on any game's page (which can also start a new list with that game), drag them into your order (the arrows on each cover do the same without dragging), and remove them with ×. Any game ShelfLife knows can be on a list, owned or not. Lists never change your recommendations: seeing games side by side helps you rate them, and only the stars count.
- **Stats:** games and hours in your library, your diary by status, games finished per year, how you rate, and your most played games.
- **Game page:** the lists it is on and **Add to list**; the store's background, developer, publisher, release date (or "Coming soon"), price, languages, length and screenshots; why the game fits you and the closest games you liked; how you played it (and how much that says you liked it); your rating, review, status and favorite; and **Games like this**, with a button to search Steam for more.
- **My taste:** the tags your taste leans toward, the games you rated low, what your taste learns from (**Ratings and play**, the default; **Ratings only**; or **Play only**), **Languages you play in** (games the store lists without any of them are left out of recommendations and Explore; none chosen means no filter), and **Hide games for adults** (see [how it works](how-it-works.md#7-games-for-adults)).

**Tags.** Pick several tags in the Library, Explore, Wishlist or Diary, and choose whether a game needs all of them or any of them. Tags match whole and ignore case, a game "has" a tag if it is among its 20 most voted, and the box suggests the tags that exist. Put a minus in front of a tag (`-Horror`) to leave out the games that have it; in **Find more games** the search then sets aside games with that tag.

The web app listens only on your own machine, answers only to `127.0.0.1` and `localhost`, and has no login, so don't expose it to a network. Run it as a single process.

**Games from other platforms.** Played something on a console, another PC store or a friend's copy? **+ Add a game from another platform** on the Library page searches Steam by name (or takes a Steam store link). Pick the platform (PlayStation, Xbox, Nintendo Switch, another PC store, mobile or other), type your hours if you know them, and it joins your library. Only games that are also on Steam can be added, so their details, tags, reviews and length come from Steam like any other game's. You stay on the search, so you can add the next game straight away; each game's store details are read in the background within seconds, and review counts and length come with the next sync. With an hour or more it counts as played: rate it and it teaches your taste like any game, and without a rating its hours do (in **Ratings and play**). Under an hour it's on your backlog and can be recommended in **From your backlog**, like an owned game you haven't started; if you did play it but don't know the hours, rate it or give it a status and it stays off the recommendations. Change the platform or the hours, or take it out of your library, on its page. If you later buy it on Steam, the next sync turns it into a normal Steam game with Steam's hours.

A game in your **family library that you played somewhere else** works the same way: find it on the same page (it says it's in your family library) and mark where you played it, or pick the platform under **Played on** on its page. It stays a family game and the Library shows it as "Family · PlayStation". Its Steam achievements are no longer read or counted, since they'd show your Steam play (often none) and pull its play score down; only the hours you type count, for wherever you played. Pick "Steam, through Family Sharing" to undo it. If the family stops sharing the game, it stays in your library as a game from that platform.

## What Sync reads

One button, nine steps, in the background with a progress bar and a Stop button:

1. **Your library:** your games, hours and when you last played each. Every sync.
2. **Your wishlist.** A wishlist Steam can't give (private, or Steam failing) is skipped and the rest carries on.
3. **Your achievements:** games you've played since they were last read, games never read, and any not read for 30 days.
4. **Tags**, straight from Steam, 200 games per request: new games, and every game once a month. A game Steam has no tags for gets SteamSpy's, or the ones on its store page.
5. **Store details** for new games and wishlist games, games that came back empty a week ago, and the 30 oldest when they are over 90 days old. About 3 seconds a game, so the very first sync takes a while.
6. **Descriptions, languages, prices and more store details**, once for games read before ShelfLife kept them.
7. **Review counts**, for games that have none or whose counts are a month old.
8. **How long other players play**: up to 500 reviews for games you've played (your hours are measured against them), 100 for the others (for their length). Only a few numbers per game are kept, never who wrote a review. Every six months.
9. **Prices** of games you don't own: your wishlist's every sync, the rest every week, a hundred games per request. Set `STEAM_COUNTRY` in `.env` to get them in your currency.

**Check everything again** ignores those limits and re-reads every game, which is slow (the page says how long). A sync saves as it goes, so the next one carries on where it stopped. It never changes your diary, never runs at the same time as a game search, and doesn't find Family Sharing games (that needs the import below).

## Command-line scripts

| Command | What it does |
|---|---|
| `python -m scripts.web` | Run the web app |
| `python -m scripts.top_games` | A quick check that your key works |
| `python -m scripts.sync [--limit N] [--fill-tags] [--descriptions]` | Import the library; fetch details and tags; or read the store text of games that lack it |
| `python -m scripts.achievements` | Achievements and when you unlocked them |
| `python -m scripts.discover [--per-tag N] [--tags "A,B"]` | Find new games you don't own (the same as Explore's button) |
| `python -m scripts.wishlist` | Read your Steam wishlist (needs only your SteamID) |
| `python -m scripts.scores [N]` | Rank your played games by engagement score |
| `python -m scripts.recommend [N]` | Print recommendations in the terminal |
| `python -m scripts.feedback not-interested "Name"` | Dismiss a game (also `undo`, `list`) |
| `python -m scripts.import_family [file]` | Import a Family Sharing export (below) |
| `python -m scripts.hours [--template]` | Enter family-game hours from a file |
| `python -m scripts.import_diary` | One-time import of old `ratings.csv` / `feedback.csv` |

Your database is `shelflife.db` in the folder you start ShelfLife from. When a new version changes its tables, it updates the database itself at start-up, keeping everything in it.

## Family Sharing (optional)

Steam's Web API only lists games on your own account. ShelfLife imports Family Sharing games the way SteamDB's browser extension does, with a script **you run yourself**, so your login token never touches ShelfLife:

1. Log in at <https://store.steampowered.com>, open DevTools (F12) > Console, paste [`tools/export_family_library.js`](../tools/export_family_library.js) and press Enter. It downloads `family-library.json`.
2. Put that file in a `data/` folder (create it) and run `python -m scripts.import_family`.
3. Sync again.

The script gets a short-lived token from the Steam store using your own cookies and calls `IFamilyGroupsService/GetSharedLibraryApps`. The token stays in the tab's memory and is never printed, saved or sent anywhere. These endpoints are undocumented by Valve and may change. The export lists which games your family members own, so keep it private.

**Hours.** Steam reports no playtime for family games, so enter what your Steam client shows: in the hours box on the game's page or in the Library, or in bulk with `python -m scripts.hours --template` (fill in `data/hours.csv`, then run `python -m scripts.hours`). Hours only apply to family games.

## If something goes wrong

- *"No games returned"*: *Game details* isn't Public in your Steam privacy settings, or `STEAM_ID` isn't the 17-digit ID.
- *"Steam rejected the API key"*: check `STEAM_API_KEY`.
- *"rate limiting"*: wait a few minutes and sync again; nothing read so far is lost.
- *Start over*: delete `shelflife.db` (your ratings live in it) and sync again.
