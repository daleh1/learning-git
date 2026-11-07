import unittest
from merch_scanner import build_flips, filter_and_sort, ge_tax, parse_gp

NOW = 1_000_000

MAPPING = [
    {"id": 1, "name": "Abyssal whip", "limit": 70, "members": True},
    {"id": 2, "name": "Old school bond", "limit": 100, "members": False},
    {"id": 3, "name": "Feather", "limit": 30000, "members": False},
    {"id": 4, "name": "Dead item", "limit": 8, "members": True},
]
LATEST = {
    "1": {"high": 1_600_000, "highTime": NOW - 30, "low": 1_550_000, "lowTime": NOW - 60},
    "2": {"high": 9_000_000, "highTime": NOW - 10, "low": 8_900_000, "lowTime": NOW - 10},
    "3": {"high": 4, "highTime": NOW - 5, "low": 3, "lowTime": NOW - 5},
    "4": {"high": 900, "highTime": NOW - 5000, "low": 100, "lowTime": NOW - 5000},
}
HOURLY = {
    "1": {"highPriceVolume": 400, "lowPriceVolume": 300},
    "2": {"highPriceVolume": 200, "lowPriceVolume": 250},
    "3": {"highPriceVolume": 900000, "lowPriceVolume": 800000},
    "4": {"highPriceVolume": 2, "lowPriceVolume": 1},
}


class Tests(unittest.TestCase):
    def test_tax(self):
        self.assertEqual(ge_tax(49), 0)
        self.assertEqual(ge_tax(1_600_000), 32_000)
        self.assertEqual(ge_tax(10_000_000_000), 5_000_000)
        self.assertEqual(ge_tax(9_000_000, "Old school bond"), 0)

    def test_parse_gp(self):
        self.assertEqual(parse_gp("20m"), 20_000_000)
        self.assertEqual(parse_gp("1.5b"), 1_500_000_000)
        self.assertEqual(parse_gp("2,500"), 2500)

    def test_margin_and_ranking(self):
        flips = {f.item_id: f for f in build_flips(MAPPING, LATEST, HOURLY, NOW)}
        self.assertEqual(flips[1].margin, 1_600_000 - 32_000 - 1_550_000)
        self.assertEqual(flips[1].max_units, 70)       # capped by buy limit
        self.assertEqual(flips[2].margin, 100_000)     # tax exempt
        ranked = filter_and_sort(flips.values())
        self.assertEqual(ranked[0].item_id, 2)
        self.assertNotIn(4, [f.item_id for f in ranked])  # stale + illiquid

    def test_budget_caps_units(self):
        flips = {f.item_id: f for f in build_flips(MAPPING, LATEST, HOURLY, NOW, budget=5_000_000)}
        self.assertEqual(flips[1].max_units, 3)
        self.assertEqual(flips[2].max_units, 0)


if __name__ == "__main__":
    unittest.main()
