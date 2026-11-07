#!/usr/bin/env python3
"""OSRS Grand Exchange flip/merch scanner.

Pulls real-time prices from the OSRS Wiki prices API (crowdsourced from
RuneLite clients) and ranks items by post-tax spread, weighted by volume
and buy limit, so you can see what is actually worth flipping right now.

Usage:
    python merch_scanner.py                       # one scan, top 25
    python merch_scanner.py --budget 20m --loop 60
    python merch_scanner.py --sort roi --min-volume 500 --f2p

Stdlib only. The Wiki asks for a descriptive User-Agent: set --user-agent
(or MERCH_UA) to something that identifies you / your tool.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass

API_ROOTS = {
    "osrs": "https://prices.runescape.wiki/api/v1/osrs",  # main game (all normal worlds share one GE)
    "dmm": "https://prices.runescape.wiki/api/v1/dmm",    # Deadman Mode (separate economy)
}

# GE tax: 2% of the sell price, rounded down, capped at 5m; items sold
# below 50 gp pay nothing. A handful of items are fully exempt.
TAX_RATE = 0.02
TAX_CAP = 5_000_000
TAX_FREE_BELOW = 50
TAX_EXEMPT_NAMES = {
    "old school bond", "chisel", "gardening trowel", "glassblowing pipe",
    "hammer", "needle", "pestle and mortar", "rake", "saw", "secateurs",
    "seed dibber", "shears", "spade", "watering can(0)",
}

BUY_LIMIT_WINDOW_H = 4  # GE buy limits reset every 4 hours


def ge_tax(sell_price: int, name: str = "") -> int:
    if sell_price < TAX_FREE_BELOW or name.lower() in TAX_EXEMPT_NAMES:
        return 0
    return min(int(sell_price * TAX_RATE), TAX_CAP)


@dataclass
class Flip:
    item_id: int
    name: str
    buy: int            # instant-sell price: what you place your buy offer at
    sell: int           # instant-buy price: what you place your sell offer at
    tax: int
    margin: int         # per item, after tax
    roi: float          # margin / buy
    limit: int | None
    hourly_volume: int  # traded units in the last hour (both sides)
    max_units: int      # units you can realistically move per 4h limit window
    potential: int      # margin * max_units
    age_s: int          # staleness of the older of the two price points
    members: bool


def fetch_json(url: str, user_agent: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_gp(text: str) -> int:
    """'20m' -> 20_000_000, '1.5b' -> 1_500_000_000, '250k' -> 250_000."""
    t = text.strip().lower().replace(",", "")
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(t[-1:], 1)
    return int(float(t[:-1] if mult != 1 else t) * mult)


def build_flips(mapping, latest, hourly, now: float, budget: int | None = None) -> list[Flip]:
    meta = {m["id"]: m for m in mapping}
    flips = []
    for sid, p in latest.items():
        item_id = int(sid)
        m = meta.get(item_id)
        high, low = p.get("high"), p.get("low")
        if not m or not high or not low or not p.get("highTime") or not p.get("lowTime"):
            continue
        name = m.get("name", str(item_id))
        tax = ge_tax(high, name)
        margin = high - tax - low
        if margin <= 0:
            continue
        h = hourly.get(sid, {})
        vol = (h.get("highPriceVolume") or 0) + (h.get("lowPriceVolume") or 0)
        limit = m.get("limit")
        # Assume you can capture ~ a quarter of one side's flow over the 4h window.
        flow_cap = int(min(h.get("highPriceVolume") or 0, h.get("lowPriceVolume") or 0)
                       * BUY_LIMIT_WINDOW_H / 4)
        units = min(limit, flow_cap) if limit else flow_cap
        if budget:
            units = min(units, budget // low)
        flips.append(Flip(
            item_id=item_id, name=name, buy=low, sell=high, tax=tax, margin=margin,
            roi=margin / low, limit=limit, hourly_volume=vol, max_units=units,
            potential=margin * units,
            age_s=int(now - min(p["highTime"], p["lowTime"])),
            members=bool(m.get("members")),
        ))
    return flips


def filter_and_sort(flips, *, min_volume=100, max_age=600, max_roi=0.5, f2p=False,
                    min_price=0, sort="potential"):
    out = [f for f in flips
           if f.hourly_volume >= min_volume
           and f.age_s <= max_age          # stale prices = fake spreads
           and f.roi <= max_roi            # absurd ROI is almost always a dead/manipulated item
           and f.buy >= min_price
           and (not f2p or not f.members)]
    key = {"potential": lambda f: f.potential, "margin": lambda f: f.margin,
           "roi": lambda f: f.roi, "volume": lambda f: f.hourly_volume}[sort]
    return sorted(out, key=key, reverse=True)


def fmt_gp(n: int) -> str:
    a = abs(n)
    if a >= 1e9: return f"{n/1e9:.2f}b"
    if a >= 1e6: return f"{n/1e6:.2f}m"
    if a >= 1e4: return f"{n/1e3:.1f}k"
    return f"{n:,}"


def render(flips, top: int) -> str:
    hdr = f"{'Item':<32}{'Buy@':>9}{'Sell@':>9}{'Tax':>7}{'Margin':>8}{'ROI':>7}{'Limit':>7}{'Vol/h':>8}{'4h pot.':>9}{'Age':>6}"
    lines = [hdr, "-" * len(hdr)]
    for f in flips[:top]:
        lines.append(
            f"{f.name[:31]:<32}{fmt_gp(f.buy):>9}{fmt_gp(f.sell):>9}{fmt_gp(f.tax):>7}"
            f"{fmt_gp(f.margin):>8}{f.roi*100:>6.1f}%{(f.limit or 0):>7}{fmt_gp(f.hourly_volume):>8}"
            f"{fmt_gp(f.potential):>9}{f.age_s:>5}s")
    return "\n".join(lines)


def scan(args) -> str:
    root = API_ROOTS[args.game]
    ua = args.user_agent
    mapping = fetch_json(f"{root}/mapping", ua)
    latest = fetch_json(f"{root}/latest", ua)["data"]
    hourly = fetch_json(f"{root}/1h", ua)["data"]
    flips = build_flips(mapping, latest, hourly, time.time(),
                        parse_gp(args.budget) if args.budget else None)
    ranked = filter_and_sort(flips, min_volume=args.min_volume, max_age=args.max_age,
                             max_roi=args.max_roi, f2p=args.f2p,
                             min_price=parse_gp(args.min_price), sort=args.sort)
    stamp = time.strftime("%H:%M:%S")
    return f"[{stamp}] {args.game.upper()} — {len(ranked)} candidates, sorted by {args.sort}\n" + render(ranked, args.top)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", choices=API_ROOTS, default="osrs")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--sort", choices=["potential", "margin", "roi", "volume"], default="potential")
    ap.add_argument("--budget", help="cash stack, e.g. 50m (caps units per item)")
    ap.add_argument("--min-volume", type=int, default=100, help="min units traded in the last hour")
    ap.add_argument("--min-price", default="0", help="ignore items cheaper than this, e.g. 1k")
    ap.add_argument("--max-age", type=int, default=600, help="max seconds since last trade on either side")
    ap.add_argument("--max-roi", type=float, default=0.5, help="drop spreads above this ROI (0.5 = 50%%)")
    ap.add_argument("--f2p", action="store_true", help="free-to-play items only")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="rescan every N seconds (>=60 recommended)")
    ap.add_argument("--user-agent", default=os.environ.get("MERCH_UA", "osrs-merch-scanner/1.0 (personal use)"))
    args = ap.parse_args(argv)

    while True:
        try:
            out = scan(args)
        except Exception as e:  # network hiccups shouldn't kill a loop
            out = f"scan failed: {e}"
        if args.loop:
            print("\033[2J\033[H", end="")
        print(out, flush=True)
        if not args.loop:
            return 0
        time.sleep(max(args.loop, 30))


if __name__ == "__main__":
    sys.exit(main())
