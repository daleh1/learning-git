#!/usr/bin/env python3
"""OSRS Grand Exchange flip/merch scanner, with a browser UI.

    python3 merch_scanner.py                    # opens the UI in your browser (Hardcore Worlds)
    python3 merch_scanner.py --game osrs        # UI, main-game economy
    python3 merch_scanner.py --cli --top 30     # plain terminal table instead

Price sources
  hardcore  Hardcore Worlds (own GE, own economy). Prices come from the community
            "Hardcore World Tool Kit" RuneLite plugin server, built only from trades
            of players who run that plugin and opted in to sharing. Thin, but real.
  osrs      Main game, from the OSRS Wiki real-time prices API (RuneLite-sourced).
  dmm       Deadman Mode, same Wiki API.

Buy limits / members flags / icons always come from the Wiki item mapping (item ids
are the same on every world type). Stdlib only, no installs needed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WIKI = "https://prices.runescape.wiki/api/v1"
# The Hardcore World Tool Kit public data API, documented at https://hardcore.malkana.dev/docs
# (open, no key; answers are cached for up to a minute; clients that ask far too often get 429/503).
# Rules we follow: each endpoint at most once a minute, never the site's paid "pro" endpoints, and
# no real-world trading use (their terms). Contact: Discord @Novith.
HC_SITE = "https://hardcore.malkana.dev"
GAMES = ("hardcore", "osrs", "dmm")

# GE tax: 2% of the sell price, rounded down, capped at 5m; items sold below 50 gp
# pay nothing; a few items are exempt. Assumed identical on Hardcore Worlds.
TAX_RATE, TAX_CAP, TAX_FREE_BELOW = 0.02, 5_000_000, 50
TAX_EXEMPT_NAMES = {
    "old school bond", "chisel", "gardening trowel", "glassblowing pipe",
    "hammer", "needle", "pestle and mortar", "rake", "saw", "secateurs",
    "seed dibber", "shears", "spade", "watering can(0)",
}
# Share of one side's 4h flow you can realistically capture with your own offers.
CAPTURE = 0.25
CACHE_SECONDS = 60  # never hit the APIs more than once a minute per game

DEFAULT_UA = os.environ.get("MERCH_UA", "osrs-merch-scanner/2.0 (personal use)")


def ge_tax(sell_price: int, name: str = "") -> int:
    if sell_price < TAX_FREE_BELOW or name.lower() in TAX_EXEMPT_NAMES:
        return 0
    return min(int(sell_price * TAX_RATE), TAX_CAP)


@dataclass
class Quote:
    """One item's market snapshot, source-independent."""
    item_id: int
    name: str
    sell_ref: int            # recent price buyers paid -> where you list your sell
    buy_ref: int             # recent price sellers got -> where you place your buy
    sell_time: int           # unix seconds of those two trades
    buy_time: int
    side_vol_24h: int        # units traded on the thinner side in 24h
    vol_24h: int             # units traded in 24h, both sides
    best_bid: int | None = None   # open order book (Hardcore only)
    best_ask: int | None = None
    bids: int = 0
    asks: int = 0
    trusted_price: int | None = None  # median of confirmed trades (Hardcore only)
    avg_sell: int | None = None   # 24h average price buyers paid (Hardcore only)
    avg_buy: int | None = None    # 24h average price sellers got (Hardcore only)
    live: bool = False


@dataclass
class Flip:
    item_id: int
    name: str
    buy: int
    sell: int
    tax: int
    margin: int
    roi: float
    limit: int | None
    vol_24h: int
    max_units: int        # units you can realistically move per 4h limit window
    potential: int        # margin * max_units
    age_s: int            # staleness of the older of the two trades
    buy_age_s: int        # seconds since the trade behind 'buy' (someone sold at that price)
    sell_age_s: int       # seconds since the trade behind 'sell' (someone bought at that price)
    members: bool
    icon: str | None
    best_bid: int | None
    best_ask: int | None
    bids: int
    asks: int
    trusted_price: int | None
    avg_margin: int | None = None  # margin using 24h averages: a steadier sanity check
    live: bool = False             # prices from the live trade feed


# ---------------------------------------------------------------- fetching

def fetch_json(url: str, user_agent: str = DEFAULT_UA, timeout: float = 20.0):
    return fetch_json_headers(url, user_agent, timeout)[0]


def fetch_json_headers(url: str, user_agent: str = DEFAULT_UA, timeout: float = 20.0, fresh: bool = False):
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if fresh:
        headers["Cache-Control"] = "no-cache"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp), dict(resp.headers)


def wiki_quotes(game: str, ua: str) -> list[Quote]:
    latest = fetch_json(f"{WIKI}/{game}/latest", ua)["data"]
    day = fetch_json(f"{WIKI}/{game}/24h", ua)["data"]
    out = []
    for sid, p in latest.items():
        if not (p.get("high") and p.get("low") and p.get("highTime") and p.get("lowTime")):
            continue
        d = day.get(sid, {})
        hv, lv = d.get("highPriceVolume") or 0, d.get("lowPriceVolume") or 0
        out.append(Quote(int(sid), "", p["high"], p["low"], p["highTime"], p["lowTime"],
                         min(hv, lv), hv + lv))
    return out


def hardcore_quotes(ua: str, raw: dict) -> list[Quote]:
    items = raw.get("items") or []
    out = []
    for s in items:
        # Prefer live fills over imported GE-history rows (which have no real time).
        sell = s.get("lastFillBuy") or s.get("lastBuy")
        sell_t = s.get("lastFillBuyTime") or s.get("lastBuyTime")
        buy = s.get("lastFillSell") or s.get("lastSell")
        buy_t = s.get("lastFillSellTime") or s.get("lastSellTime")
        if not (sell and buy and sell_t and buy_t):
            continue
        vol = int(s.get("vol24") or 0)
        out.append(Quote(
            int(s["id"]), s.get("name") or "", int(sell), int(buy), int(sell_t), int(buy_t),
            vol // 2, vol,
            best_bid=s.get("trustedBestBid") or s.get("bestBid"),
            best_ask=s.get("trustedBestAsk") or s.get("bestAsk"),
            bids=int(s.get("bids") or 0), asks=int(s.get("asks") or 0),
            trusted_price=s.get("trustedPrice"),
            avg_sell=s.get("avgBuy24"), avg_buy=s.get("avgSell24"), live=bool(s.get("live")),
        ))
    return out


def describe_error(e: Exception) -> str:
    """A one-line, human explanation of a failed request."""
    code = getattr(e, "code", None)
    if code is not None:
        hdrs = getattr(e, "headers", None) or {}
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        except Exception:
            pass
        hint = {429: "too many requests (rate limited)", 503: "server busy", 403: "forbidden (blocked)",
                404: "not found"}.get(code, "")
        ra = hdrs.get("Retry-After")
        return f"HTTP {code} {hint}" + (f", Retry-After {ra}s" if ra else "") + (f", body: {body}" if body else "")
    text = str(getattr(e, "reason", e))
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return ("SSL certificate error: run '/Applications/Python 3.*/Install Certificates.command' "
                "(python.org installs on macOS need this once)")
    return f"{type(e).__name__}: {text}"


class RateLimited(Exception):
    pass


class HardcoreFeed:
    """Hardcore prices from the public data API, within its documented limits.

    - /v1/items: every item's summary (the per-item prices in it lag ~40 min), every LIST_EVERY seconds.
    - /v1/recent?kinds=fill&limit=200&after=<newest id seen>: the LIVE feed of trades, once a minute.
      Each fill updates that item's live buy/sell price, so actively traded items get current prices
      on top of the delayed list. `after` means we only ever receive what is new.
    - At most one request per update() call and never two within MIN_GAP seconds; 429/503 back off
      (Retry-After, else 30 s doubling to 5 min). State is saved to disk so restarts don't refetch.
    """
    LIST_EVERY = 5 * 60
    RECENT_EVERY = 60        # the API caches answers for a minute: asking faster gains nothing
    SWAP_EVERY = 60 * 60
    MIN_GAP = 20
    FILL_TTL = 2 * 60 * 60   # forget live fills older than this

    def __init__(self, ua: str = DEFAULT_UA, state_path: Path | None = None, base: str = HC_SITE,
                 clock=time.time):
        self.ua, self.base, self.clock = ua, base, clock
        self.path = state_path or Path.home() / ".osrs_merch_state_v3.json"
        self.lock = threading.RLock()
        self.st = {"list": None, "list_at": 0, "fills": {}, "last_event": 0, "recent_at": 0,
                   "recent_stats": None, "swap": None, "swap_at": 0, "last_request": 0,
                   "backoff_until": 0, "failures": 0, "log": [], "last_error": None}
        try:
            self.st.update(json.loads(self.path.read_text()))
        except Exception:
            pass

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.st))
        except Exception:
            pass

    def blocked_for(self) -> int:
        now = self.clock()
        return int(max(self.st["backoff_until"] - now, self.st["last_request"] + self.MIN_GAP - now, 0))

    def _get(self, path: str) -> dict:
        now = self.clock()
        if now < self.st["backoff_until"]:
            raise RateLimited(f"rate limited, retrying in {int(self.st['backoff_until'] - now)}s")
        self.st["last_request"] = now
        try:
            data = fetch_json(f"{self.base}{path}", self.ua)
        except Exception as e:
            self._log(path, now, error=e)
            if getattr(e, "code", None) in (429, 503):
                retry = 0
                try:
                    retry = int((getattr(e, "headers", None) or {}).get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
                wait = max(min(30 * 2 ** min(self.st["failures"], 4), 300), min(retry, 3600))
                self.st["failures"] += 1
                self.st["backoff_until"] = now + wait
            raise
        self._log(path, now)
        self.st["failures"] = 0
        self.st["backoff_until"] = 0
        self.st["last_error"] = None
        return data

    def _log(self, path: str, started: float, error: Exception | None = None):
        """Keep the last 25 requests (time, path, result) for the debug views."""
        entry = {"t": int(started), "path": path.split("?")[0], "ms": int((self.clock() - started) * 1000)}
        if error is None:
            entry["result"] = "ok"
        else:
            entry["result"] = describe_error(error)
            self.st["last_error"] = f"{entry['path']}: {entry['result']}"
        self.st["log"] = (self.st.get("log") or [])[-24:] + [entry]

    def _ingest(self, recent: dict):
        """Fold a /v1/recent page into per-item live prices."""
        fills = self.st["fills"]
        for ev in recent.get("events") or []:
            if ev.get("kind") != "fill" or not ev.get("price") or not ev.get("ts"):
                continue
            self.st["last_event"] = max(self.st["last_event"], int(ev.get("id") or 0))
            f = fills.setdefault(str(ev["itemId"]), {"name": ev.get("name"), "n": 0})
            # side "buy": someone bought at this price (your sell target); "sell": someone sold (your buy target)
            key = "buy" if ev.get("side") == "buy" else "sell"
            if ev["ts"] >= f.get(key + "Ts", 0):
                f[key], f[key + "Ts"] = int(ev["price"]), int(ev["ts"])
            f["n"] += 1
        cutoff = self.clock() - self.FILL_TTL
        for k in [k for k, f in fills.items() if max(f.get("buyTs", 0), f.get("sellTs", 0)) < cutoff]:
            del fills[k]
        self.st["recent_stats"] = recent.get("stats")

    def update(self, force: bool = False) -> str | None:
        """Make at most one request, if one is due and allowed. Returns a warning, if any."""
        with self.lock:
            if self.blocked_for() > 0:
                return None
            now = self.clock()
            try:
                if not self.st["list"] or now - self.st["list_at"] >= self.LIST_EVERY:
                    self.st["list"] = self._get("/v1/items?include=fill,trust")
                    self.st["list_at"] = now
                elif now - self.st["recent_at"] >= (self.MIN_GAP if force else self.RECENT_EVERY):
                    after = f"&after={self.st['last_event']}" if self.st["last_event"] else ""
                    self._ingest(self._get(f"/v1/recent?kinds=fill&limit=200{after}"))
                    self.st["recent_at"] = now
                elif now - self.st["swap_at"] >= self.SWAP_EVERY:
                    self.st["swap_at"] = now
                    self.st["swap"] = self._get("/v1/swap-rate")
            except RateLimited as e:
                return str(e)
            except Exception as e:
                return (f"Price site said 429 Too Many Requests; pausing {self.blocked_for()}s."
                        if getattr(e, "code", None) == 429 else f"Price site request failed: {e}")
            finally:
                self._save()
            return None

    def items(self) -> list[dict]:
        """The delayed list, with any newer live fill prices laid over it (marked live=True)."""
        with self.lock:
            merged = {str(it["id"]): dict(it) for it in (self.st["list"] or {}).get("items") or []}
            for k, f in self.st["fills"].items():
                it = merged.get(k)
                if it is None:
                    continue
                for side, field, fill in (("buy", "lastBuy", "lastFillBuy"), ("sell", "lastSell", "lastFillSell")):
                    seen = max(it.get(field + "Time") or 0, it.get(fill + "Time") or 0)
                    if f.get(side + "Ts", 0) > seen:
                        it[field], it[field + "Time"] = f[side], f[side + "Ts"]
                        it[fill], it[fill + "Time"] = f[side], f[side + "Ts"]
                        it["live"] = True
            return list(merged.values())

    def status(self) -> dict:
        now = self.clock()
        lst = self.st["list"] or {}
        stats = self.st.get("recent_stats") or {}
        return {
            "list_age_s": int(now - self.st["list_at"]) if self.st["list_at"] else None,
            "list_server_time": lst.get("time"),
            "next_list_s": max(0, int(self.st["list_at"] + self.LIST_EVERY - now)),
            "recent_age_s": int(now - self.st["recent_at"]) if self.st["recent_at"] else None,
            "live_items": len(self.st["fills"]),
            "reporters24": stats.get("reporters24"),
            "backoff_s": int(max(0, self.st["backoff_until"] - now)),
            "last_error": self.st.get("last_error"),
            "log": self.st.get("log") or [],
        }


_mapping_cache: dict = {}


def wiki_mapping(ua: str) -> dict[int, dict]:
    if not _mapping_cache:
        _mapping_cache.update({m["id"]: m for m in fetch_json(f"{WIKI}/osrs/mapping", ua)})
    return _mapping_cache


# ---------------------------------------------------------------- analysis

def build_flips(quotes: list[Quote], mapping: dict[int, dict], now: float,
                budget: int | None = None, keep_unprofitable: bool = False) -> list[Flip]:
    flips = []
    for q in quotes:
        m = mapping.get(q.item_id, {})
        name = m.get("name") or q.name or str(q.item_id)
        tax = ge_tax(q.sell_ref, name)
        margin = q.sell_ref - tax - q.buy_ref
        if q.buy_ref <= 0 or (margin <= 0 and not keep_unprofitable):
            continue
        limit = m.get("limit")
        flow = int(q.side_vol_24h / 6 * CAPTURE)  # 24h -> one 4h window, your share
        units = min(limit, flow) if limit else flow
        if budget:
            units = min(units, budget // q.buy_ref)
        flips.append(Flip(
            item_id=q.item_id, name=name, buy=q.buy_ref, sell=q.sell_ref, tax=tax,
            margin=margin, roi=margin / q.buy_ref, limit=limit, vol_24h=q.vol_24h,
            max_units=units, potential=margin * units,
            age_s=max(0, int(now - min(q.sell_time, q.buy_time))),
            buy_age_s=max(0, int(now - q.buy_time)), sell_age_s=max(0, int(now - q.sell_time)),
            members=bool(m.get("members")), icon=m.get("icon"),
            best_bid=q.best_bid, best_ask=q.best_ask, bids=q.bids, asks=q.asks,
            trusted_price=q.trusted_price,
            avg_margin=(q.avg_sell - ge_tax(q.avg_sell, name) - q.avg_buy)
            if q.avg_sell and q.avg_buy else None,
            live=q.live,
        ))
    return flips


DEFAULTS = {  # per-economy filter defaults: Hardcore data is much thinner
    "hardcore": dict(min_volume=5, max_age=6 * 3600, max_roi=1.0),
    "osrs": dict(min_volume=2000, max_age=900, max_roi=0.5),
    "dmm": dict(min_volume=200, max_age=1800, max_roi=0.5),
}


def filter_and_sort(flips, *, min_volume=0, max_age=10**9, max_roi=10.0, f2p=False,
                    min_price=0, sort="potential"):
    out = [f for f in flips
           if f.margin > 0 and f.vol_24h >= min_volume and f.age_s <= max_age and f.roi <= max_roi
           and f.buy >= min_price and (not f2p or not f.members)]
    return sorted(out, key=lambda f: getattr(f, sort), reverse=True)


_feed: HardcoreFeed | None = None


def get_feed(ua: str) -> HardcoreFeed:
    global _feed
    if _feed is None:
        _feed = HardcoreFeed(ua)
    return _feed


def scan(game: str, ua: str = DEFAULT_UA, force: bool = False, feed: HardcoreFeed | None = None) -> dict:
    """Fetch (politely) and analyse one economy. Returns a JSON-ready dict for the UI."""
    warnings = []
    try:
        mapping = wiki_mapping(ua)
    except Exception as e:
        mapping = {}
        warnings.append(f"Wiki item mapping unavailable ({e}); no buy limits/icons.")
    now = time.time()
    status, swap, server_time = None, None, None
    if game == "hardcore":
        feed = feed or get_feed(ua)
        w = feed.update(force=force)
        if w:
            warnings.append(w)
        if not feed.st["list"]:
            st = feed.status()
            why = w or (f"last error: {st['last_error']}" if st["last_error"] else None)
            if st["backoff_s"]:
                why = f"paused for {st['backoff_s']}s after: {st['last_error']}"
            raise RuntimeError("No Hardcore data yet" + (f" ({why})" if why else "; it arrives within a few seconds")
                               + ". Run: python3 osrs_merch/merch_scanner.py --diagnose")
        quotes = hardcore_quotes(ua, raw={"items": feed.items()})
        status, swap = feed.status(), feed.st.get("swap")
        server_time = status["list_server_time"]
    else:
        quotes = wiki_quotes(game, ua)
    # Keep unprofitable items too: a manual margin check in the UI can make them profitable.
    flips = build_flips(quotes, mapping, now, keep_unprofitable=True)
    newest = max((max(q.buy_time, q.sell_time) for q in quotes), default=None)
    if game == "hardcore" and not quotes:
        warnings.append("The Hardcore price server returned no items with both a buy and a sell price yet.")
    return {"game": game, "fetched_at": int(now), "items_seen": len(quotes),
            "server_time": server_time, "newest_trade_age_s": int(now - newest) if newest else None,
            "flips": [asdict(f) for f in flips], "defaults": DEFAULTS[game],
            "warnings": warnings, "swap_rate": swap, "feed": status}


# ---------------------------------------------------------------- terminal output

def parse_gp(text: str) -> int:
    """'20m' -> 20_000_000, '1.5b' -> 1_500_000_000, '250k' -> 250_000."""
    t = text.strip().lower().replace(",", "")
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(t[-1:], 1)
    return int(float(t[:-1] if mult != 1 else t) * mult)


def fmt_gp(n) -> str:
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1e9: return f"{n/1e9:.2f}b"
    if a >= 1e6: return f"{n/1e6:.2f}m"
    if a >= 1e4: return f"{n/1e3:.1f}k"
    return f"{n:,}"


def fmt_age(s: int) -> str:
    return f"{s}s" if s < 60 else f"{s//60}m" if s < 3600 else f"{s//3600}h"


def render(flips, top: int) -> str:
    hdr = (f"{'Item':<30}{'Buy@':>9}{'Sell@':>9}{'Tax':>7}{'Margin':>8}{'ROI':>7}"
           f"{'Limit':>7}{'Vol24h':>8}{'4h pot.':>9}{'Age':>6}")
    lines = [hdr, "-" * len(hdr)]
    for f in flips[:top]:
        lines.append(
            f"{f.name[:29]:<30}{fmt_gp(f.buy):>9}{fmt_gp(f.sell):>9}{fmt_gp(f.tax):>7}"
            f"{fmt_gp(f.margin):>8}{f.roi*100:>6.1f}%{(f.limit or 0):>7}{fmt_gp(f.vol_24h):>8}"
            f"{fmt_gp(f.potential):>9}{fmt_age(f.age_s):>6}")
    return "\n".join(lines)


def run_diagnose(args) -> int:
    """Explain the connection to the Hardcore API: saved state, recent requests, one live health check."""
    feed = get_feed(args.user_agent)
    now = int(time.time())
    st = feed.status()
    print(f"Python {sys.version.split()[0]} on {sys.platform}; state file {feed.path} "
          f"({'exists' if feed.path.exists() else 'not created yet'})")
    print(f"API base: {feed.base}")
    print(f"Item list: {'fetched ' + fmt_age(st['list_age_s']) + ' ago' if st['list_age_s'] is not None else 'NEVER fetched'}"
          f"; live items: {st['live_items']}")
    print(f"Backoff: {'paused ' + fmt_age(st['backoff_s']) if st['backoff_s'] else 'none'}"
          f"; last error: {st['last_error'] or 'none'}")
    if st["log"]:
        print("\nRecent requests (newest last):")
        for e in st["log"][-12:]:
            print(f"  {fmt_age(max(0, now - e['t'])):>5} ago  {e['path']:<16} {e['ms']:>5} ms  {e['result']}")
    print("\nLive check (GET /v1/health, the cheapest endpoint):")
    if st["backoff_s"]:
        print(f"  skipped: still paused for {st['backoff_s']}s (asking now would only extend the lockout)")
    else:
        t0 = time.time()
        try:
            data, hdrs = fetch_json_headers(f"{feed.base}/v1/health", args.user_agent)
            print(f"  OK in {int((time.time() - t0) * 1000)} ms: {data}")
            print(f"  server clock vs yours: {int(data.get('time', now)) - now:+d}s" if isinstance(data, dict) else "")
        except Exception as e:
            print(f"  FAILED: {describe_error(e)}")
    print("\nWhat it means:")
    if st["backoff_s"] or "429" in (st["last_error"] or ""):
        print("  The API is rate limiting your IP. Stop all scanner copies, wait out the pause, start ONE copy.")
    elif st["list_age_s"] is None and st["last_error"]:
        print("  The item list has never loaded: see the last error above (network, SSL, or blocked).")
    elif st["list_age_s"] is None:
        print("  Nothing has been requested yet. Start the scanner (one copy) and give it a minute.")
    else:
        print("  Connection looks fine.")
    return 0


def run_debug(args) -> int:
    """Show the feed's state and the data it holds for items matching a name (one request at most)."""
    feed = get_feed(args.user_agent)
    w = feed.update()
    now = int(time.time())
    st = feed.status()
    print(f"Your clock: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}   state file: {feed.path}")
    if w:
        print(f"! {w}")
    print(f"Backoff: {st['backoff_s']}s remaining" if st["backoff_s"] else "Backoff: none")
    lt = st["list_server_time"]
    print(f"Item list: fetched {fmt_age(st['list_age_s']) + ' ago' if st['list_age_s'] is not None else 'never'}"
          + (f", site snapshot {fmt_age(now - lt)} old" if lt else ""))
    print(f"Live feed: {st['live_items']} items with live prices, polled "
          f"{fmt_age(st['recent_age_s']) + ' ago' if st['recent_age_s'] is not None else 'never'}")
    want = args.debug.lower()
    for it in feed.items():
        if want in (it.get("name") or "").lower():
            print(("\n[LIVE] " if it.get("live") else "\n[LIST] ") + json.dumps(it, indent=2))
            for k, v in it.items():
                if k.endswith("Time") and v:
                    print(f"  {k:<14} = {fmt_age(max(0, now - int(v)))} ago")
    return 0


def run_cli(args) -> int:
    while True:
        try:
            data = scan(args.game, args.user_agent)
            d = DEFAULTS[args.game]
            flips = [Flip(**f) for f in data["flips"]]
            if args.budget:
                b = parse_gp(args.budget)
                for f in flips:
                    f.max_units = min(f.max_units, b // f.buy)
                    f.potential = f.margin * f.max_units
            ranked = filter_and_sort(
                flips, min_volume=args.min_volume if args.min_volume is not None else d["min_volume"],
                max_age=d["max_age"], max_roi=d["max_roi"], f2p=args.f2p, sort=args.sort)
            out = (f"[{time.strftime('%H:%M:%S')}] {args.game.upper()}: {len(ranked)} candidates "
                   f"of {data['items_seen']} items, sorted by {args.sort}\n" + render(ranked, args.top))
            for w in data["warnings"]:
                out += f"\n! {w}"
        except Exception as e:
            out = f"scan failed: {e}"
        if args.loop:
            print("\033[2J\033[H", end="")
        print(out, flush=True)
        if not args.loop:
            return 0
        time.sleep(max(args.loop, CACHE_SECONDS))


# ---------------------------------------------------------------- browser UI

UI_HTML = Path(__file__).with_name("ui.html")


class Cache:
    def __init__(self, ua: str):
        self.ua, self.lock, self.data = ua, threading.Lock(), {}

    blocked_until = 0.0  # set when the server answers 429 Too Many Requests

    def get(self, game: str, force: bool = False) -> dict:
        if game == "hardcore":
            return scan(game, self.ua, force=force)
        with self.lock:
            hit = self.data.get(game)
            fresh_enough = hit and time.time() - hit[0] < CACHE_SECONDS
            if hit and (fresh_enough and not force or time.time() < self.blocked_until):
                return hit[1]
        try:
            result = scan(game, self.ua, force=force)
        except Exception as e:
            if getattr(e, "code", None) == 429:
                retry = getattr(e, "headers", {}).get("Retry-After") if getattr(e, "headers", None) else None
                self.blocked_until = time.time() + (int(retry) if str(retry or "").isdigit() else 180)
            if not hit:
                raise
            # Keep showing the last good data rather than an error.
            stale = dict(hit[1])
            stale["warnings"] = list(stale.get("warnings", [])) + [
                f"Couldn't refresh ({e}); showing data fetched at {time.strftime('%H:%M', time.localtime(hit[0]))}."]
            return stale
        with self.lock:
            self.data[game] = (time.time(), result)
        return result


def make_handler(cache: Cache):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, UI_HTML.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/flips":
                qs = urllib.parse.parse_qs(url.query)
                game = qs.get("game", ["hardcore"])[0]
                if game not in GAMES:
                    return self._send(400, b'{"error":"unknown game"}', "application/json")
                try:
                    body = json.dumps(cache.get(game, force="force" in qs)).encode()
                    return self._send(200, body, "application/json")
                except Exception as e:
                    return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
            self._send(404, b"not found", "text/plain")

    return Handler


def _feed_pump(ua: str):
    while True:
        try:
            get_feed(ua).update()
        except Exception:
            pass
        time.sleep(15)


def run_ui(args) -> int:
    if args.game == "hardcore":
        threading.Thread(target=_feed_pump, args=(args.user_agent,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Cache(args.user_agent)))
    url = f"http://127.0.0.1:{server.server_address[1]}/?game={args.game}"
    print(f"Merch scanner running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", choices=GAMES, default="hardcore")
    ap.add_argument("--cli", action="store_true", help="print a terminal table instead of opening the UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--top", type=int, default=25, help="(cli) rows to show")
    ap.add_argument("--sort", choices=["potential", "margin", "roi", "vol_24h"], default="potential")
    ap.add_argument("--budget", help="(cli) cash stack, e.g. 50m")
    ap.add_argument("--min-volume", type=int, help="(cli) min units traded in 24h")
    ap.add_argument("--f2p", action="store_true", help="(cli) free-to-play items only")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="(cli) rescan every N seconds")
    ap.add_argument("--debug", metavar="NAME", help="print raw Hardcore server data for items matching NAME")
    ap.add_argument("--diagnose", action="store_true", help="explain the connection to the Hardcore API")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args(argv)
    if args.diagnose:
        return run_diagnose(args)
    if args.debug:
        return run_debug(args)
    return run_cli(args) if args.cli else run_ui(args)


if __name__ == "__main__":
    sys.exit(main())
