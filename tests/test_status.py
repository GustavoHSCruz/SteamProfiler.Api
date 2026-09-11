"""The public status payload, and the line it is not allowed to cross.

/healthz and /status read the same modules and answer two different audiences.
The risk is not that /status is wrong today: it is that somebody adds a figure
to it later because it was one line away and looked harmless, and a country
breakdown on a site this size, some weeks, is not harmless.

So the first test is written as a list of names that must never appear
anywhere in the payload, at any depth, rather than as a copy of today's shape.
A new figure is welcome; a new figure called `by_country` is not.
"""
import tempfile
import unittest
from pathlib import Path

import api
import art
import census
import guard
import houses
import meta
import proton

# Every module public_status() reads that keeps something on disk. Each is
# pointed at a temporary directory for the length of a case, the way
# test_cards.py and test_collection.py already do it, so the suite runs the
# same on a laptop as in the container where DATA_DIR actually exists.
ON_DISK = (census, houses, proton, meta)


def names(value, into=None):
    """Every key name in a payload, at any depth."""
    into = set() if into is None else into
    if isinstance(value, dict):
        for key, inner in value.items():
            into.add(key)
            names(inner, into)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            names(inner, into)
    return into


class PublicStatusTest(unittest.TestCase):
    # Three groups, and each one is a different promise.
    #
    #   the census      what a person is in: the classes, the origins, the
    #                   per-profile rows. census.report() holds all of it and
    #                   answers to the owner's panel alone.
    #   the gate        the addresses being tracked, the shut-outs, the
    #                   appeals. Operational, and a probe for whether a given
    #                   address is currently blocked.
    #   the allowance   the absolute budget and what has been spent against
    #                   it. The page prints a percentage and a word; the two
    #                   numbers behind them stay here.
    FORBIDDEN = {
        "by_class", "by_country", "by_region", "visitors", "subjects",
        "subject_totals", "per_day", "history", "steamid", "ip_hash",
        "hash", "country", "region", "confidence", "class",
        "tracked", "blocked", "denied", "bans", "appeals", "exempt_local",
        "ephemeral", "pending_subjects",
        "steam_calls_today", "budget_calls", "BUDGET",
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.saved = [(mod, mod.DATA_DIR, mod.DB_PATH) for mod in ON_DISK]
        self.saved_art = (art.DATA_DIR, art.ART_DIR)
        self.saved_ready = [(mod, getattr(mod, "_ready", None))
                            for mod in ON_DISK if hasattr(mod, "_ready")]
        for mod in ON_DISK:
            mod.DATA_DIR = path
            mod.DB_PATH = path / mod.DB_PATH.name
            if hasattr(mod, "_ready"):
                mod._ready = False
        art.DATA_DIR = path
        art.ART_DIR = path / "art"
        census.init()

    def tearDown(self):
        for mod, data_dir, db_path in self.saved:
            mod.DATA_DIR, mod.DB_PATH = data_dir, db_path
        for mod, ready in self.saved_ready:
            mod._ready = ready
        art.DATA_DIR, art.ART_DIR = self.saved_art
        self.temp.cleanup()

    def test_payload_carries_nothing_private(self):
        found = names(api.public_status())
        self.assertEqual(sorted(found & self.FORBIDDEN), [])

    def test_traffic_is_counts_only(self):
        traffic = census.public()
        self.assertEqual(
            sorted(traffic),
            ["addresses", "began_at", "lookups", "requests", "weeks",
             "window_days"])
        for week in traffic["weeks"]:
            self.assertEqual(sorted(week), ["addresses", "began_at", "requests"])

    def test_budget_is_a_share_and_never_the_allowance(self):
        steam = api.public_status()["steam"]
        self.assertIn(steam["budget"], ("normal", "tight", "spent"))
        self.assertLessEqual(steam["budget_used"], 100)
        # The percentage may round to the allowance, never to a call count.
        self.assertNotEqual(steam["budget_used"], guard.BUDGET)

    def test_census_public_survives_a_missing_database(self):
        """A status page that 500s because the census table is not there yet
        would report an outage the service is not having."""
        original = census.DB_PATH
        census.DB_PATH = original.parent / "census-does-not-exist" / "x.db"
        try:
            self.assertIsNone(census.public())
        finally:
            census.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
