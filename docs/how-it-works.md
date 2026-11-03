# How recommendations work

ShelfLife learns your taste from your ratings and from how you played your games, then ranks every game you haven't played by how well it fits that taste and how well it is reviewed. Everything below runs on your own computer.

## 1. How much your play says you liked a game

`shelflife/play.py`. Hours alone mislead: two hours can be all of a short game, and a hundred hours of an endless one can be ordinary. So each signal is placed among comparable play, as a percentile:

| Signal | Compared with |
|---|---|
| Hours | your other played games, and (up to half) the hours the game's own reviewers have played it |
| Achievements unlocked | your other games (games with 10+ achievements) |
| Rarest achievement you hold | your other games (rarer = more devoted) |
| When you played | a small shift up for current play, down for play long past, from your achievement unlock dates |

A game's play score is the average of the signals it has (a missing one is left out, not counted as zero), so 50% is your typical played game. Other players' hours come from up to 500 of the game's reviews, positive and negative reviewers weighed by the game's real share, kept as percentiles so a few players with thousands of idle hours move only the top end. They count for at most half, because people who love a game keep playing it long after the end. Games under an hour are your backlog, not a verdict.

The older **engagement score** (`shelflife/score.py`: points for hours, achievements, rare achievements, current play and recency) is still shown in the Library and on game pages.

## 2. What your taste learns from

`shelflife/recommend.py`. Only four things, each with one meaning:

- **Your star ratings.** A rating of 3 stars or more says how much you liked a game, `(rating − 3) / 2` from 0 to +1, and games like it rise. A rating under 3 stars says "not this game": it pushes down only games very like it (a sequel, a near-clone), never its whole genre. Disliking one horror game says nothing about horror.
- **"Not interested"** hides a game and counts as a weaker 2-star rating: it pushes down only its close look-alikes, a little less than a real 2-star rating would, because it can also mean "not now" or "too expensive". A rating you give the game wins over it.
- **Your wishlist** counts as a weaker 3.5-star rating, in every mode: like "not interested" the other way round, it lifts only its close look-alikes, so a few hundred wishlisted games can't swamp your ratings, and a game never lifts itself (the wishlist page is still ranked by everything else). A rating, "not interested" or your play wins over it. On the owner's games it moved the check below from +0.302 to +0.309 on ratings and from +0.408 to +0.417 on ranked games.
- **Games you played but didn't rate**, in **Ratings and play** (the default). In **Play only** every game you played counts this way, rated or not, and your star ratings are ignored; **Ratings only** leaves play out. Playing a game says you probably liked it, so it counts as the rating your played games usually get; how much you played it (the play score above) says how sure that is. Little play means "not sure", never "didn't like it".

Favorites and statuses (playing, played, dropped) are diary records: they never change your taste, and a game in your diary isn't recommended.

The look-alike step (dislikes, "not interested" and the wishlist) only looks at pairs of games at least 0.2 alike: anything less would count under 1% as much as a sequel, and leaving those pairs out keeps scoring fast with a long wishlist.

## 3. Learning your taste

`shelflife/learned.py`. Every game is described two ways: by its tags, each counting by how relevant it is to the game (its votes relative to the game's top tag, squared, for the top 15 tags) and how rare it is in your library; and by the words of the store's "About this game" text that tell games apart (store boilerplate dropped; words used by at least 3 games and at most 15% of them; a description left with fewer than 20 such words, such as one written in another script, counts as none). Two games are as alike as those descriptions are, half tags and half words, so two games with different tags but the same story still match, and a sequel matches its original by the names they share. A ridge regression then learns, from every game you have a verdict on, which kinds of games you like, and predicts that for every other game. Because it is fitted to your games one by one, a game very close to a favourite stays close to it. Nothing is sent anywhere: it is a few hundred games of arithmetic, kept between pages until your data changes.

**How good is it?** `python tools/evaluate.py` predicts every game you rated (and every game you ranked, if you keep `data/rankings.csv`) with everything about that game left out, and compares the predictions with your own verdicts. On the owner's 144 ratings and 52 ranked games the rank correlation was +0.455 and +0.41 learning from play too, and +0.452 and +0.38 from ratings alone (an earlier design, with three modes and dislikes spreading to whole genres, scored about +0.41 to +0.43 and +0.31). Play alone predicts ratings much less well (about +0.08 and +0.28 on later data): how much you play a game says less about how much you liked it than a rating does. One person's games, so take the exact numbers lightly.

## 4. Finding new games

Explore's **Find more games** button (and `python -m scripts.discover`) works like this:

1. **Steam lists the games under a tag**, with their review counts and their own tags: pages of 1,000 in Steam's popularity order, until a page brings fewer than 20 games with enough reviews, at most 5, so a few seconds a tag.
2. That list is loose (any game with even a few votes for the tag is in it), so **games you already have and games with under 300 reviews are dropped**, and the rest are ranked by the lower bound of their positive-review share (more reviews rank higher at equal praise).
3. Going down the ranking, with the tags taking turns, **a game is kept only if the searched tag is among its 10 most voted**.
4. Kept games get their store details fetched, about 3 seconds a game, and then take part in the rankings.

For "games like this one", the tags searched are that game's most distinctive ones and a game must also be at least 15% similar to it.

When Steam doesn't know a tag or can't be reached, SteamSpy's list is used instead. It has no tags, so each game's are read one at a time, and the games set aside are remembered so they aren't read again. SteamSpy matches tag names in a particular letter case (and a few under older names), so several spellings are tried. Without tags, the search reads SteamSpy's popularity-ordered list (up to 3 pages of 1,000 games, a minute apart as SteamSpy asks, and only as many as needed) and keeps the best-reviewed games you don't have, with no tag check.

## 5. Scoring

```
score = predicted_liking(game) × quality
quality = (Wilson lower bound of the positive-review share) ^ 2
```

Explore and the Wishlist rank by this score by default ("Best fit"), and offer "Highest match" to rank by the liking alone. The predicted liking, stretched so that a close match reads as one, is shown as the "match" on every pick; it is 0 for a game unlike anything you have a verdict on, and below 0 for one very like a game you rated low (its card names that game). The Wilson bound is cautious with few reviews: about 0.9 at 95% positive, 0.5 at 70%, 0.36 with no review data. Candidates are backlog games (owned, under an hour played), family games you haven't played, and games you don't own (300+ reviews); other editions of games you already have are skipped, and so are games for adults while that switch is on.

## 6. Game length

A game's length is estimated from how long its reviewers played it: their median, scaled to about a "main story plus extras" time. For endless games (multiplayer, sandbox and the like), it shows how long players typically play instead. It is shown on cards and game pages, and Library, Explore and Wishlist can filter by it (under 5 h, 5 to 15 h, 15 to 40 h, over 40 h).

## 7. Games for adults

**Hide games for adults** (on My taste, on by default) leaves these games out of recommendations and Explore:

- games Steam labels "adult only";
- games whose community tags are mostly about sexual content.

Games that merely have adult scenes (Cyberpunk 2077, Baldur's Gate 3) stay. So do games players tagged "Hentai" as a joke when the store says they have no sexual content. Your library, diary and wishlist page always show every game.
