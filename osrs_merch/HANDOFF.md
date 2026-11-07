# OSRS GE Merch Scanner: handoff notes

Read this first. It is everything needed to keep working on this project.

## What it is
A small personal tool that finds Grand Exchange flipping ("merching") opportunities in
Old School RuneScape. It reads price data and ranks items by the profit of buying at
the recent low price and selling at the recent high price, after GE tax. It is
**read-only decision support**: it never controls the game or places offers.

The default economy is **Hardcore Worlds**, a game mode launched on 4 Nov 2026 with
its **own separate GE and economy**. Its prices are very different from the main game.
As a sanity check, a bond costs about 1m gp there versus about 9m on the main game.

## Files (all in `osrs_merch/`)
| File | Purpose |
|---|---|
| `merch_scanner.py` | Everything in Python, stdlib only (no pip installs): fetching, analysis, terminal output, and a local web server for the UI. |
| `ui.html` | Single-page browser UI, plain HTML/CSS/JS with no frameworks, served by `merch_scanner.py`. |
| `test_merch_scanner.py` | unittest tests. They mock `fetch_json`, so they need no network. |
| `HANDOFF.md` | This file. |

## Running it (macOS: use `python3`, not `python`)
```
python3 osrs_merch/merch_scanner.py                  # opens the UI at http://127.0.0.1:8765 (Hardcore)
python3 osrs_merch/merch_scanner.py --game osrs      # UI for the main-game economy (also: dmm)
python3 osrs_merch/merch_scanner.py --cli --loop 60  # terminal table instead of the UI
python3 osrs_merch/merch_scanner.py --debug "death rune"   # raw Hardcore server data for an item
cd osrs_merch && python3 -m unittest -v test_merch_scanner # run the tests
```

## Data sources
**Hardcore Worlds** (`--game hardcore`, the default) use the community "Hardcore World Tool
Kit" RuneLite plugin server (https://github.com/Novafifth/hardcore-world-tool-kit). The
data comes only from players who run that plugin and opted in to sharing, so it is thin
and partial. Reads are plain unauthenticated GETs, and the tool must stay polite: at most
one request a minute, cached in `Cache`, with `CACHE_SECONDS = 60`.
- Base URL: `https://hardcore-ge-tracker-107023686748.europe-north1.run.app`
- Every request takes `world=9000`, the fixed economy id the plugin uses for all Hardcore
  Worlds, and `include=trust,fill`.
- `GET /v1/items?world=9000&include=trust,fill` returns `{"time": <unix s>, "items": [ItemSummary...]}`.
  Each ItemSummary has: `id, name, lastBuy, lastBuyTime, lastSell, lastSellTime, bestBid, bestAsk,
  bids, asks, vol24, trustedPrice, trustedVotes, trustedPriceTime, confVol24, unconfVol24,
  trustedBestBid, trustedBestAsk, lastFillBuy, lastFillBuyTime, lastFillSell, lastFillSellTime`.
  Timestamps are **unix seconds**. The `lastFill*` fields are live fills. Plain `last*` can come
  from imported GE-history rows, which have no real time, so the code prefers `lastFill*`.
- `GET /v1/item/{id}?world=9000&limit=40` gives one item's detail: summary, order book, recent trades.
- `GET /v1/item/{id}/series?world=9000&range=24h|7d|30d|90d|1y&step=5m|1h|6h|24h` gives price history.
- `GET /v1/recent?world=9000&limit=60&kinds=fill` is the feed of recent trades.
- `GET /v1/swap-rate?world=9000` returns `{rate, rateLow, rateHigh, main, hardcore}`, the bond price in each economy.
- These shapes were taken from the plugin's Java source (`MarketClient.java`, `MarketData.java`)
  and **have not been verified against the live server yet**.

**Main game / Deadman** (`--game osrs|dmm`) use the OSRS Wiki real-time prices API:
`https://prices.runescape.wiki/api/v1/{osrs|dmm}/latest`, `/24h` and `/mapping`.
- In `/latest`, `high` is the instant-buy price, so it is the sell target, and `low` is the
  instant-sell price, so it is the buy target. Each comes with `highTime` / `lowTime`.
- The Wiki requires a descriptive User-Agent. It is set with `--user-agent` or the `MERCH_UA` env var.
- `/osrs/mapping` supplies buy limits, the members flag and icon names for **every** economy,
  because item ids are the same everywhere.

## Core logic (`merch_scanner.py`)
- `Quote` is the source-independent snapshot of one item. `hardcore_quotes()` and `wiki_quotes()` build it.
- `build_flips()` turns quotes into `Flip` rows:
  - `margin = sell - ge_tax(sell) - buy`
  - `roi = margin / buy`
  - `max_units = min(buy_limit, side_vol_24h / 6 * CAPTURE)`, optionally capped by budget. `CAPTURE = 0.25`,
    and `/6` turns 24h of volume into one 4-hour buy-limit window.
  - `potential = margin * max_units`, shown as "4h profit".
  - `age_s` is the age of the older side. `buy_age_s` and `sell_age_s` are per side.
- GE tax in `ge_tax()` is 2% of the sell price, rounded down and capped at 5m. Items sold under
  50 gp pay none, and the names in `TAX_EXEMPT_NAMES` are exempt. The code assumes the same rules
  apply on Hardcore Worlds; that is unverified.
- `DEFAULTS` holds per-economy filter defaults. Hardcore uses looser values because its data is thinner.
- `scan(game)` returns the JSON the UI consumes: `flips`, `items_seen`, `server_time`,
  `newest_trade_age_s`, `swap_rate`, `warnings`, `defaults`.
- The web server is `ThreadingHTTPServer` bound to 127.0.0.1. Routes are `/` (ui.html) and
  `/api/flips?game=...[&force=1]`.

## UI (`ui.html`)
- Economy toggle, sortable columns (click a header), and filters for search, budget, minimum
  24h volume, maximum trade age, minimum price, maximum ROI and F2P. Filters are saved per
  economy in localStorage. Auto-refreshes every 60s.
- Budget is applied **client-side**: `units = min(max_units, floor(budget / buy))`.
- **Security rule:** item names come from other players via the server, so they must never be
  inserted as HTML. Always use `textContent` or the `el()` helper, never `innerHTML`.
- Hardcore-only extras: the "Book bid / ask" column (turns red when bid >= ask, which means a
  stale offer), a "confirmed" tag when `trusted_price` exists, and the bond swap rate in the banner.

## Known issues / open questions (current work)
1. **Trade ages look too old.** Every item's last trade seems to be at least about 40 minutes
   old, even for busy items. Possible causes:
   - (a) few plugin users;
   - (b) the server publishes with a delay or caches its answers;
   - (c) the user's computer clock is wrong.

   Diagnostics were added: per-side ages, "newest trade in data" in the banner, a
   server-vs-local clock warning, and `--debug NAME`. **Next step:** run
   `--debug "death rune"` and interpret the output. If the newest trade in the whole response
   is about 40 minutes old, the delay is on the server's side.
2. API response shapes are unverified against the live server (see above).
3. Whether GE tax applies on Hardcore Worlds is unverified.
4. Volume can be inflated by imported GE-history rows, so `max_units` may be overestimated.

## Ideas for next steps
- Paper-trading simulator: poll every minute, simulate offers at the suggested prices, and
  count a fill only when later trades cross that price. Report simulated profit per hour,
  fill rate and the best items. This validates the strategy without touching the game.
- Trade journal: log manual trades in the UI and compare real profit with predicted profit.
- An item detail panel using `/v1/item/{id}/series` for a price chart.

## Hard constraints
- **Do not build anything that controls the game client.** No screen, mouse or keyboard
  automation, no remote-desktop input, no auto-placing GE offers. That is botting and against
  Jagex's rules. The owner previously asked for this and it was declined. Automated trading is
  only acceptable against a private/mock server's own HTTP API, never by driving a game client.
- Keep it stdlib-only. Don't hit the APIs more than once a minute.
- Keep the tests passing, and add tests for new logic, using mocked `fetch_json`.
- Git: work on branch `claude/osrs-ge-merching-spreads-0fyqh5`.
