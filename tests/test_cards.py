import json
import tempfile
import unittest
from pathlib import Path

import cards
import community
import fx


class FakeClock:
    """A clock that only moves when somebody sleeps. Copied rather than shared
    with test_community.py: these files import nothing of each other's, so that
    each of them runs on its own the way check.sh discovers them."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CardSetTest(unittest.TestCase):
    """The market is never called here. What is worth testing is the parsing
    and the four states a row can be in, and both of those are decided after
    the answer arrives."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = cards.DATA_DIR
        self.old_db_path = cards.DB_PATH
        self.old_ready = cards._ready
        self.old_get = cards._get
        cards.DATA_DIR = Path(self.temp.name)
        cards.DB_PATH = cards.DATA_DIR / "meta.db"
        cards._ready = False
        # The pace and the cooldown belong to community.py, which is shared with
        # inv.py and outlives any one test. Assigning cards._last_at here used to
        # reset them; after the move it would set attributes nobody reads, and
        # the suite would pass while a cooldown leaked from one case into the
        # next.
        #
        # The clock goes with it. A set of more than one page really does wait
        # the interval between pages, and on a real clock that was eight seconds
        # of the suite spent proving the pacer works - which is test_community's
        # job, not this file's.
        clock = FakeClock()
        old_time = (community._clock, community._sleep)
        community._clock, community._sleep = clock.monotonic, clock.sleep
        community.reset()

        def restore():
            community._clock, community._sleep = old_time
            community.reset()

        self.addCleanup(restore)
        cards.init()
        # An answer about a set carries the day's rate, so fx reads the same
        # database here that it shares with cards in production. Nothing writes
        # a rate into it, which is the state a fresh server is in too: the
        # dollar answers alone until the first fetch lands.
        self.old_fx = (fx.DATA_DIR, fx.DB_PATH, fx._ready)
        fx.DATA_DIR = cards.DATA_DIR
        fx.DB_PATH = cards.DB_PATH
        fx._ready = False
        fx._forget()
        fx.init()

    def tearDown(self):
        cards.DATA_DIR = self.old_data_dir
        cards.DB_PATH = self.old_db_path
        cards._ready = self.old_ready
        cards._get = self.old_get
        fx.DATA_DIR, fx.DB_PATH, fx._ready = self.old_fx
        fx._forget()
        self.temp.cleanup()

    @staticmethod
    def answer(names_and_prices, total=None):
        results = [{
            "name": f"{name} (Trading Card)",
            "hash_name": f"570-{name} (Trading Card)",
            "sell_price": price,
            "sell_listings": 100,
            "asset_description": {"icon_url": "TOKEN", "type": "Dota 2 Trading Card"},
        } for name, price in names_and_prices]
        return {"success": True, "total_count": total if total is not None else len(results),
                "results": results}

    def test_reads_a_set_and_totals_one_of_each_card(self):
        cards._get = lambda url: self.answer([("Tiny", 6), ("Razor", 4)])
        got = cards.set_of(570, wait=0)
        self.assertEqual(got["state"], "fresh")
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["cost"], 10)
        self.assertEqual([c["name"] for c in got["cards"]], ["Tiny", "Razor"])
        self.assertTrue(got["cards"][0]["icon"].endswith("/TOKEN/96fx96f"))

    def test_a_card_nobody_sells_leaves_the_set_without_a_price(self):
        """A total missing one card is not what the set costs, and printing it
        anyway would quote a badge cheaper than it can be made."""
        cards._get = lambda url: self.answer([("Tiny", 6), ("Razor", 0)])
        got = cards.set_of(570, wait=0)
        self.assertEqual(got["count"], 2)
        self.assertIsNone(got["cost"])
        self.assertIsNone(got["cards"][1]["cents"])

    def test_a_game_without_cards_is_an_answer_and_not_a_hole(self):
        cards._get = lambda url: {"success": True, "total_count": 0, "results": []}
        got = cards.set_of(480, wait=0)
        self.assertEqual(got["state"], "none")
        self.assertEqual(got["count"], 0)

    def test_the_same_card_twice_is_counted_once(self):
        """Paging a list the market is re-sorting under us is the one way a
        card arrives twice, and a set with two Tinys in it prices a badge
        nobody has to buy."""
        pages = [self.answer([("Tiny", 6)], total=2), self.answer([("Tiny", 6)], total=2)]
        cards._get = lambda url: pages.pop(0) if pages else None
        got = cards.set_of(570, wait=0)
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["cost"], 6)

    def test_a_market_that_says_nothing_leaves_yesterdays_answer_alone(self):
        cards._get = lambda url: self.answer([("Tiny", 6)])
        cards.set_of(570, wait=0)
        cards._get = lambda url: None
        with cards._connect() as con:
            con.execute("UPDATE card_sets SET seen_at = '2020-01-01T00:00:00+00:00'")
        got = cards.set_of(570, wait=0)
        self.assertEqual(got["state"], "stale")
        self.assertTrue(got["stale"])
        self.assertEqual(got["cost"], 6)

    def test_throttled_keeps_the_row_and_queues_the_app(self):
        def refuse(url):
            raise cards.Throttled(url)

        cards._get = refuse
        got = cards.set_of(570, wait=0)
        self.assertEqual(got["state"], "unknown")
        self.assertIn(570, cards._queued)
        with cards._queue_lock:
            cards._wanted.clear()
            cards._queued.clear()


class HasCardsTest(unittest.TestCase):
    """Three answers, not two: a game the store cache has not reached yet is
    not a game without cards."""

    def test_reads_the_store_category(self):
        self.assertTrue(cards.has_cards({"categories": [{"id": 29, "name": "Steam Trading Cards"}]}))
        self.assertFalse(cards.has_cards({"categories": [{"id": 1, "name": "Multi-player"}]}))
        self.assertIsNone(cards.has_cards(None))
        self.assertIsNone(cards.has_cards({}))


if __name__ == "__main__":
    unittest.main()
