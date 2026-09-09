import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STEAM_API_KEY", "test")
os.environ.setdefault("STEAM_ID", "76561198000000000")

import cards
import community
import fetch
import fx
import inv
import meta

from test_inv import answer, asset, card


class FakeClock:
    """A clock that only moves when somebody sleeps. Copied rather than shared,
    like the one in test_cards.py: these files import nothing of each other's
    scaffolding so each of them runs on its own."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CollectionTest(unittest.TestCase):
    """Neither Steam nor the market is called here. What is worth testing is
    the join: which of the four awkward set states a game lands in, and the one
    rule that must never soften - a cost is the whole cost or it is nothing."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = (cards.DATA_DIR, cards.DB_PATH, cards._ready,
                      meta.DATA_DIR, meta.DB_PATH, meta._ready,
                      fx.DATA_DIR, fx.DB_PATH, fx._ready, inv._fetch, cards._get)
        path = Path(self.temp.name)
        for mod in (cards, meta, fx):
            mod.DATA_DIR = path
            mod.DB_PATH = path / "meta.db"
            mod._ready = False
        fx._forget()
        inv.forget()
        # Two card sets in one test are two market reads an interval apart, and
        # on a real clock the second one would find no slot and answer off an
        # empty disk. That is the pacer working, and proving it is
        # test_community's job rather than this file's.
        self.clock = FakeClock()
        self.old_time = (community._clock, community._sleep)
        community._clock, community._sleep = self.clock.monotonic, self.clock.sleep
        community.reset()
        meta.init()
        cards.init()
        fx.init()
        self.addCleanup(self.restore)

    def restore(self):
        (cards.DATA_DIR, cards.DB_PATH, cards._ready,
         meta.DATA_DIR, meta.DB_PATH, meta._ready,
         fx.DATA_DIR, fx.DB_PATH, fx._ready, inv._fetch, cards._get) = self.saved
        community._clock, community._sleep = self.old_time
        fx._forget()
        inv.forget()
        community.reset()
        cards._wanted.clear()
        cards._queued.clear()
        self.temp.cleanup()

    def hold(self, *pairs):
        """Put cards in the inventory: (appid, name, amount)."""
        assets, descs = [], []
        for n, (appid, name, amount) in enumerate(pairs, start=1):
            assets.append(asset(n, amount))
            descs.append(card(appid, name, n))
        inv._fetch = lambda steamid: (200, answer(assets, descs))

    def price(self, appid, *names_and_cents):
        """Put a card set on disk, the way a market read would have.

        The clock moves past one interval first. Two sets in one test are two
        reads the pacer really would put eight seconds apart, and without this
        the second one finds no slot, answers off an empty disk, and the test
        silently measures an unknown set instead of the one it priced."""
        self.clock.now += community.INTERVAL + 1
        cards._get = lambda url: {
            "success": True, "total_count": len(names_and_cents),
            "results": [{"name": f"{appid}-{name} (Trading Card)",
                         "hash_name": f"{appid}-{name}",
                         "sell_price": cents, "sell_listings": 3,
                         "asset_description": {"icon_url": "I",
                                               "market_hash_name": f"{appid}-{name}"}}
                        for name, cents in names_and_cents],
        }
        cards.set_of(appid, wait=0)

    def one(self, got, appid):
        return next(r for r in got["games"] if r["appid"] == appid)

    def test_it_says_what_finishing_costs_and_not_what_a_set_costs(self):
        """The whole point. Three of five in hand means the figure is the two
        that are missing, not the price of all five."""
        self.hold((620, "A", 1), (620, "B", 1), (620, "C", 1))
        self.price(620, ("A", 5), ("B", 5), ("C", 5), ("D", 4), ("E", 6))
        row = self.one(fetch.build_collection("1"), 620)
        self.assertEqual((row["have"], row["need"]), (3, 2))
        self.assertEqual(row["cost_to_complete"], 10)

    def test_a_missing_card_nobody_sells_makes_the_cost_nothing_at_all(self):
        """Same rule cards._do_set applies to a set's own cost. A total that
        silently drops an unpriced card quotes a badge cheaper than it can be
        made, which is worse than declining to quote it."""
        self.hold((620, "A", 1))
        self.price(620, ("A", 5), ("B", None), ("C", 7))
        row = self.one(fetch.build_collection("1"), 620)
        self.assertIsNone(row["cost_to_complete"])
        self.assertEqual(row["unpriced"], 1)
        self.assertEqual(fetch.build_collection("1")["filling"]["unpriceable"], 1)

    def test_a_full_set_costs_zero_and_zero_is_a_real_answer(self):
        """Nothing missing means the badge can be made today for nothing, and
        that has to read as 0 rather than as None."""
        self.hold((620, "A", 2), (620, "B", 1))
        self.price(620, ("A", 5), ("B", 5))
        row = self.one(fetch.build_collection("1"), 620)
        self.assertEqual(row["need"], 0)
        self.assertEqual(row["cost_to_complete"], 0)
        self.assertEqual(row["sets_held"], 1)
        self.assertEqual(row["dupes"], 1)

    def test_a_set_nobody_has_priced_yet_is_not_a_free_set(self):
        """`unknown` must never be totalled as zero: a cost that grows while
        the crawl catches up is the failure build_cards already avoids."""
        self.hold((620, "A", 1))
        got = fetch.build_collection("1")
        row = self.one(got, 620)
        self.assertEqual(row["set"], "unknown")
        self.assertIsNone(row["cost_to_complete"])
        self.assertIsNone(row["need"])
        self.assertEqual(got["filling"]["unknown_sets"], 1)
        self.assertIsNone(got["totals"]["complete_all"])

    def test_a_market_that_said_no_cards_loses_to_cards_in_hand(self):
        """The market says an app has no cards while the inventory is holding
        some, which happens when a set was added after the row was written. The
        inventory is the harder evidence, so the row says "not known yet"
        rather than "no cards"."""
        self.hold((620, "A", 1))
        cards._get = lambda url: {"success": True, "total_count": 0, "results": []}
        cards.set_of(620, wait=0)
        row = self.one(fetch.build_collection("1"), 620)
        self.assertEqual(row["set"], "unknown")
        self.assertIsNone(row["cost_to_complete"])

    def test_yesterdays_prices_are_still_answered_and_are_marked(self):
        """Refusing to answer on a stale set would be worse than answering:
        the prices moved by a cent, not by an order of magnitude."""
        self.hold((620, "A", 1))
        self.price(620, ("A", 5), ("B", 9))
        with cards._connect() as con:
            con.execute("UPDATE card_sets SET seen_at = '2020-01-01T00:00:00+00:00'")
        got = fetch.build_collection("1")
        row = self.one(got, 620)
        self.assertTrue(row["stale"])
        self.assertEqual(row["cost_to_complete"], 9)
        self.assertEqual(got["filling"]["stale_sets"], 1)

    def test_a_private_inventory_draws_nothing_and_claims_nothing(self):
        inv._fetch = lambda steamid: (403, None)
        got = fetch.build_collection("1")
        self.assertEqual(got["state"], "private")
        self.assertEqual(got["games"], [])
        self.assertIsNone(got["totals"]["complete_all"])

    def test_the_closest_row_comes_first(self):
        """A game one card away for four cents is the entire point of the
        panel, so it cannot be somewhere down the list."""
        self.hold((620, "A", 1), (730, "A", 1), (730, "B", 1), (730, "C", 1))
        self.price(620, ("A", 5), ("B", 5), ("C", 5))
        self.price(730, ("A", 5), ("B", 5), ("C", 5), ("D", 4))
        got = fetch.build_collection("1")
        self.assertEqual(got["closest"][0]["appid"], 730)
        self.assertEqual(got["closest"][0]["cost_to_complete"], 4)


if __name__ == "__main__":
    unittest.main()
