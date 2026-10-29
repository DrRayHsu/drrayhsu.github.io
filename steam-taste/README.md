# steam-taste

Analyzes your Steam library to show what you actually like to play, based on hours played, games you drop, wishlist gaps, achievement completion and (optionally) purchase history. It's meant as raw material for deciding what game to make.

Uses only the Python 3.9+ standard library, with nothing to install.

## Run it

1. Get a Steam Web API key at <https://steamcommunity.com/dev/apikey> (any domain name works).
2. Set **Profile → Edit Profile → Privacy Settings → Game details** to **Public**. You can switch it back afterwards.
3. Run:

```bash
export STEAM_API_KEY=your_key
export STEAM_ID=your_steamid64_or_profile_url
python3 steam_taste.py
```

The first run tags every game through SteamSpy at about one request per second, so a 500-game library takes about 10 minutes. Responses are cached in `.cache/`, so later runs are fast. Use `--refresh` to re-fetch everything.

Output:

- `out/report.html`: open in a browser.
- `out/taste_summary.json`: compact findings to paste into Claude for brainstorming.

### Optional: purchase history

Open <https://store.steampowered.com/account/history>, click **Load more transactions** until it stops, then save the page (Ctrl/Cmd+S, "Webpage, HTML only"). Then run:

```bash
python3 steam_taste.py --purchase-history ~/Downloads/history.html
```

This adds your refunds and a breakdown of purchases by month of the year, including how many of those games you never launched.

## What it measures

| Section | Question it answers |
|---|---|
| Lift | Which tags get more of your play time than their share of your library predicts? |
| Core games | What do your most-played games have in common? |
| Wishlist vs. reality | What catches your eye but doesn't hold you, and the reverse? |
| Bounce | Which kinds of games do you drop within 2 hours? |
| Finishing | Which kinds of games do you complete (by achievements)? |
| Drift | Which tags are rising in the last 12 months? |
| Buying habits | Do sale-season purchases go unplayed? What did you refund? |

Tags are SteamSpy's top 12 user tags per game, weighted by votes. Play time is weighted by log(1 + hours) so that one 2,000-hour game doesn't swamp the rest. Useful flags: `--min-support` (minimum games per tag, default 3), `--bounce-minutes` (default 120), `--max-achievement-games` (0 to skip achievements).

## Tests

```bash
python3 -m unittest test_steam_taste
```
