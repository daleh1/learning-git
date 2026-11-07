import json
import tempfile
import threading
import urllib.error
from pathlib import Path
import unittest
import urllib.request
from unittest import mock

import merch_scanner as ms

NOW = 1_000_000
MAPPING = [
    {"id": 1, "name": "Abyssal whip", "limit": 70, "members": True, "icon": "Abyssal whip.png"},
    {"id": 2, "name": "Old school bond", "limit": 100, "members": False},
    {"id": 3, "name": "Feather", "limit": 30000, "members": False},
]
HC_ITEMS = {"time": NOW, "items": [
    # live fill prices preferred over GE-history ones
    {"id": 1, "name": "Abyssal whip", "lastBuy": 1, "lastBuyTime": NOW - 9999, "lastSell": 1, "lastSellTime": NOW - 9999,
     "lastFillBuy": 300_000, "lastFillBuyTime": NOW - 60, "lastFillSell": 280_000, "lastFillSellTime": NOW - 120,
     "bestBid": 279_000, "bestAsk": 301_000, "bids": 3, "asks": 2, "vol24": 1200, "trustedPrice": 290_000},
    {"id": 2, "name": "Old school bond", "lastBuy": 1_050_000, "lastBuyTime": NOW - 30,
     "lastSell": 1_000_000, "lastSellTime": NOW - 30, "vol24": 480, "avgBuy24": 1_040_000, "avgSell24": 1_010_000},
    {"id": 3, "name": "Feather", "lastBuy": 5, "lastBuyTime": NOW - 5, "vol24": 9999},  # no sell side -> skipped
]}


def fake_fetch(url, *a, **k):
    if url.endswith("/osrs/mapping"):
        return MAPPING
    if "/v1/items" in url:
        assert "world=9000" in url
        return HC_ITEMS
    if "/v1/swap-rate" in url:
        return {"rate": 0.11, "main": 9_000_000, "hardcore": 1_000_000}
    raise AssertionError(url)


class Tests(unittest.TestCase):
    def setUp(self):
        ms._mapping_cache.clear()

    def test_tax(self):
        self.assertEqual(ms.ge_tax(49), 0)
        self.assertEqual(ms.ge_tax(1_600_000), 32_000)
        self.assertEqual(ms.ge_tax(10_000_000_000), 5_000_000)
        self.assertEqual(ms.ge_tax(9_000_000, "Old school bond"), 0)

    def test_parse_gp(self):
        self.assertEqual(ms.parse_gp("20m"), 20_000_000)
        self.assertEqual(ms.parse_gp("1.5b"), 1_500_000_000)
        self.assertEqual(ms.parse_gp("2,500"), 2500)

    def test_hardcore_quotes_and_flips(self):
        with mock.patch.object(ms, "fetch_json", fake_fetch):
            quotes = ms.hardcore_quotes("ua", HC_ITEMS)
            mapping = ms.wiki_mapping("ua")
        self.assertEqual([q.item_id for q in quotes], [1, 2])
        self.assertEqual((quotes[0].sell_ref, quotes[0].buy_ref), (300_000, 280_000))
        flips = {f.item_id: f for f in ms.build_flips(quotes, mapping, NOW)}
        whip = flips[1]
        self.assertEqual(whip.margin, 300_000 - 6_000 - 280_000)
        self.assertEqual(whip.max_units, 25)          # 600 per side / 6 * 0.25
        self.assertEqual(whip.age_s, 120)
        self.assertEqual((whip.buy_age_s, whip.sell_age_s), (120, 60))
        self.assertEqual(flips[2].margin, 50_000)     # bonds are tax exempt
        self.assertEqual(flips[2].max_units, 10)
        self.assertEqual(flips[2].avg_margin, 30_000)
        self.assertIsNone(whip.avg_margin)

    def test_budget_caps_units(self):
        with mock.patch.object(ms, "fetch_json", fake_fetch):
            flips = ms.build_flips(ms.hardcore_quotes("ua", HC_ITEMS), ms.wiki_mapping("ua"), NOW, budget=600_000)
        self.assertEqual({f.item_id: f.max_units for f in flips}, {1: 2, 2: 0})

    def test_ui_server_serves_page_and_api(self):
        ms._feed = ms.HardcoreFeed("ua", Path(tempfile.mkdtemp()) / "state.json")
        with mock.patch.object(ms, "fetch_json", fake_fetch):
            srv = ms.ThreadingHTTPServer(("127.0.0.1", 0), ms.make_handler(ms.Cache("ua")))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                self.assertIn(b"GE <span>Merch</span>", urllib.request.urlopen(base + "/").read())
                data = json.load(urllib.request.urlopen(base + "/api/flips?game=hardcore"))
                self.assertEqual(len(data["flips"]), 2)
                self.assertEqual(data["server_time"], NOW)
            finally:
                srv.shutdown()


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.t = [NOW]
        self.calls = []
        self.state = Path(tempfile.mkdtemp()) / "state.json"
        self.fail_with = None

    def fetch(self, url, *a, **k):
        self.calls.append(url)
        if self.fail_with:
            raise self.fail_with
        if "ids=" in url:
            return {"time": NOW, "items": [dict(HC_ITEMS["items"][0], lastFillBuy=310_000)]}
        if "swap-rate" in url:
            return {"main": 9_000_000, "hardcore": 1_000_000}
        return HC_ITEMS

    def feed(self):
        return ms.HardcoreFeed("ua", self.state, clock=lambda: self.t[0])

    def test_one_request_per_update_and_spacing(self):
        f = self.feed()
        with mock.patch.object(ms, "fetch_json", self.fetch):
            f.update([1, 2])                  # first: the full list
            f.update([1, 2])                  # too soon: nothing
            self.t[0] += f.MIN_GAP
            f.update([1, 2])                  # shortlist, batched in one request
            self.t[0] += f.MIN_GAP
            f.update([1, 2])                  # live not due yet -> swap rate
            self.t[0] += f.MIN_GAP
            f.update([1, 2])                  # nothing due
        self.assertEqual(len(self.calls), 3)
        self.assertIn("ids=1,2&", self.calls[1])
        live = {it["id"]: it for it in f.items()}
        self.assertTrue(live[1]["live"])
        self.assertEqual(live[1]["lastFillBuy"], 310_000)
        self.assertNotIn("live", live[2])

    def test_429_backs_off_and_persists(self):
        f = self.feed()
        self.fail_with = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "120"}, None)
        with mock.patch.object(ms, "fetch_json", self.fetch):
            w = f.update([1])
            self.assertIn("429", w)
            self.t[0] += 100
            self.fail_with = None
            f2 = self.feed()                  # restart: the backoff is remembered
            f2.update([1])
            self.assertEqual(len(self.calls), 1)
            self.t[0] += 30
            f2.update([1])
            self.assertEqual(len(self.calls), 2)

    def test_long_retry_after_turns_off_shortlist(self):
        f = self.feed()
        with mock.patch.object(ms, "fetch_json", self.fetch):
            f.update([1])                     # list ok
            self.t[0] += f.MIN_GAP
            self.fail_with = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "1800"}, None)
            f.update([1])                     # shortlist -> long lockout
            self.fail_with = None
            self.t[0] += 1800
            f.update([1])                     # quiet: no shortlist; swap rate instead
        self.assertNotIn("ids=", self.calls[-1])
        self.assertGreater(f.status()["quiet_s"], 0)

    def test_shortlist_skips_negative_average(self):
        with mock.patch.object(ms, "fetch_json", fake_fetch):
            ms._mapping_cache.clear()
            flips = ms.build_flips(ms.hardcore_quotes("ua", HC_ITEMS), ms.wiki_mapping("ua"), NOW)
        flips[1].avg_margin = -5
        self.assertEqual(ms.pick_shortlist(flips, 5), [flips[0].item_id])


if __name__ == "__main__":
    unittest.main()
