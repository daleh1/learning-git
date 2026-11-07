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
HC_SERVER = "https://hardcore-ge-tracker-107023686748.europe-north1.run.app"
HC_ECONOMY = 9000  # the plugin's fixed economy id for all Hardcore Worlds
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


def hardcore_raw(ua: str, base: str = HC_SERVER) -> dict:
    q = urllib.parse.urlencode({"world": HC_ECONOMY, "include": "trust,fill"})
    return fetch_json(f"{base}/v1/items?{q}", ua)


def hardcore_quotes(ua: str, base: str = HC_SERVER, raw: dict | None = None) -> list[Quote]:
    items = (raw if raw is not None else hardcore_raw(ua, base)).get("items") or []
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
            avg_sell=s.get("avgBuy24"), avg_buy=s.get("avgSell24"),
        ))
    return out


def hardcore_swap_rate(ua: str, base: str = HC_SERVER):
    try:
        return fetch_json(f"{base}/v1/swap-rate?world={HC_ECONOMY}", ua)
    except Exception:
        return None


_mapping_cache: dict = {}


def wiki_mapping(ua: str) -> dict[int, dict]:
    if not _mapping_cache:
        _mapping_cache.update({m["id"]: m for m in fetch_json(f"{WIKI}/osrs/mapping", ua)})
    return _mapping_cache


# ---------------------------------------------------------------- analysis

def build_flips(quotes: list[Quote], mapping: dict[int, dict], now: float,
                budget: int | None = None) -> list[Flip]:
    flips = []
    for q in quotes:
        m = mapping.get(q.item_id, {})
        name = m.get("name") or q.name or str(q.item_id)
        tax = ge_tax(q.sell_ref, name)
        margin = q.sell_ref - tax - q.buy_ref
        if margin <= 0 or q.buy_ref <= 0:
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
           if f.vol_24h >= min_volume and f.age_s <= max_age and f.roi <= max_roi
           and f.buy >= min_price and (not f2p or not f.members)]
    return sorted(out, key=lambda f: getattr(f, sort), reverse=True)


def scan(game: str, ua: str = DEFAULT_UA) -> dict:
    """Fetch and analyse one economy. Returns a JSON-ready dict for the UI."""
    warnings = []
    try:
        mapping = wiki_mapping(ua)
    except Exception as e:
        mapping = {}
        warnings.append(f"Wiki item mapping unavailable ({e}); no buy limits/icons.")
    server_time = None
    if game == "hardcore":
        raw = hardcore_raw(ua)
        server_time = raw.get("time")
        if server_time and server_time > 1e11:  # milliseconds
            server_time //= 1000
        quotes = hardcore_quotes(ua, raw=raw)
    else:
        quotes = wiki_quotes(game, ua)
    now = time.time()
    flips = build_flips(quotes, mapping, now)
    newest = max((max(q.buy_time, q.sell_time) for q in quotes), default=None)
    result = {"game": game, "fetched_at": int(now), "items_seen": len(quotes),
              "server_time": server_time, "newest_trade_age_s": int(now - newest) if newest else None,
              "flips": [asdict(f) for f in flips], "defaults": DEFAULTS[game],
              "warnings": warnings, "swap_rate": None}
    if game == "hardcore":
        result["swap_rate"] = hardcore_swap_rate(ua)
        if not quotes:
            warnings.append("The Hardcore price server returned no items with both a buy and a sell price yet.")
    return result


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


def _newest(obj) -> int | None:
    """Newest unix-seconds timestamp anywhere in a JSON value (keys ending in Time/ts/time)."""
    best = None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and (k.endswith("Time") or k in ("ts", "time")) and 1e9 < v < 1e11:
                best = max(best or 0, int(v))
            elif isinstance(v, (dict, list)):
                n = _newest(v)
                if n:
                    best = max(best or 0, n)
    elif isinstance(obj, list):
        for v in obj:
            n = _newest(v)
            if n:
                best = max(best or 0, n)
    return best


def run_debug(args) -> int:
    """Compare how fresh each Hardcore server endpoint is, for items matching a name."""
    ua = args.user_agent
    inc = f"world={HC_ECONOMY}&include=trust,fill"
    raw, hdrs = fetch_json_headers(f"{HC_SERVER}/v1/items?{inc}", ua, fresh=True)
    now = int(time.time())
    print(f"Your computer's clock: {now} ({time.strftime('%Y-%m-%d %H:%M:%S %Z')})")
    print(f"Server HTTP Date header: {hdrs.get('Date')}  Cache-Control: {hdrs.get('Cache-Control')}  Age: {hdrs.get('Age')}")
    st = raw.get("time")
    print(f"Full list  /v1/items: 'time' {fmt_age(now - st) if st else '?'} ago, newest trade "
          f"{fmt_age(now - _newest(raw.get('items')))} ago, {len(raw.get('items') or [])} items")
    want = args.debug.lower()
    matches = [s for s in raw.get("items") or [] if want in (s.get("name") or "").lower()][:3]
    for s in matches:
        iid = s["id"]
        print(f"\n=== {s.get('name')} (id {iid}) ===")
        print(json.dumps(s, indent=2))
        for label, url in [("Filtered   /v1/items?ids", f"{HC_SERVER}/v1/items?ids={iid}&{inc}"),
                           ("Item view  /v1/item/{id}", f"{HC_SERVER}/v1/item/{iid}?limit=40&{inc}")]:
            try:
                d, h = fetch_json_headers(url, ua, fresh=True)
                n = _newest(d)
                t = d.get("time") if isinstance(d, dict) else None
                print(f"{label}: newest trade {fmt_age(now - n) if n else '?'} ago"
                      + (f", 'time' {fmt_age(now - t)} ago" if isinstance(t, int) else "")
                      + f"  (Cache-Control: {h.get('Cache-Control')}, Age: {h.get('Age')})")
                trades = d.get("trades") or d.get("recent") or [] if isinstance(d, dict) else []
                for tr in trades[:5]:
                    if isinstance(tr, dict) and tr.get("ts"):
                        print(f"    {tr.get('side', '?'):<4} {tr.get('qty', '?'):>6} @ {tr.get('price')}  {fmt_age(now - int(tr['ts']))} ago")
            except Exception as e:
                print(f"{label}: failed ({e})")
    print("\nIf the filtered/item endpoints are much fresher than the full list, the full list is cached"
          " and the scanner should fetch candidates by id. If all are equally old, the server delays public data.")
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

    def get(self, game: str, force: bool = False) -> dict:
        with self.lock:
            hit = self.data.get(game)
            if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
        result = scan(game, self.ua)
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


def run_ui(args) -> int:
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
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args(argv)
    if args.debug:
        return run_debug(args)
    return run_cli(args) if args.cli else run_ui(args)


if __name__ == "__main__":
    sys.exit(main())
