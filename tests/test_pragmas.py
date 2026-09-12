import tempfile
import unittest
from pathlib import Path

import bans
import blocks
import blog
import cards
import census
import fx
import houses
import meta
import proton
import store


# SQLite answers `PRAGMA synchronous` as a number.
OFF, NORMAL, FULL = 0, 1, 2

# Which setting each database is meant to run on, and why. The caches are
# everything Steam, the storefront or ProtonDB will answer a second time if this
# file loses its last commit to a power cut; the four on FULL hold something
# that only exists here.
#
# This is a test rather than a comment because the cost of it silently going
# back to the default is not a wrong answer - it is 135ms a commit on the
# server's disk, measured, which is felt as the site answering 503 under a
# crawl and is never traced back to a missing line.
EXPECTED = {
    meta: NORMAL,
    cards: NORMAL,
    fx: NORMAL,
    proton: NORMAL,
    houses: NORMAL,
    census: NORMAL,
    blog: FULL,
    store: FULL,
    bans: FULL,
    blocks: FULL,
}


class PragmaTest(unittest.TestCase):
    """No real database is touched: every module is pointed at a temporary file
    and asked what it opens with."""

    def pragmas(self, module):
        with tempfile.TemporaryDirectory() as tmp:
            was_dir = getattr(module, "DATA_DIR", None)
            was_path = module.DB_PATH
            if was_dir is not None:
                module.DATA_DIR = Path(tmp)
            module.DB_PATH = Path(tmp) / was_path.name
            try:
                with module._connect() as con:
                    return (con.execute("PRAGMA synchronous").fetchone()[0],
                            con.execute("PRAGMA journal_mode").fetchone()[0])
            finally:
                if was_dir is not None:
                    module.DATA_DIR = was_dir
                module.DB_PATH = was_path

    def test_every_database_opens_on_the_setting_it_was_given(self):
        for module, want in EXPECTED.items():
            with self.subTest(module=module.__name__):
                sync, journal = self.pragmas(module)
                self.assertEqual(sync, want)
                # NORMAL only means "fsync at the checkpoint instead of at every
                # commit" while the journal is WAL. On a rollback journal the
                # same word means something else entirely, and something this
                # service has not agreed to.
                self.assertEqual(journal.lower(), "wal")

    def test_nothing_is_running_with_durability_off(self):
        """OFF is the one setting that trades corruption for speed. Nothing here
        is fast enough to be worth a truncated file."""
        for module in EXPECTED:
            with self.subTest(module=module.__name__):
                self.assertNotEqual(self.pragmas(module)[0], OFF)


if __name__ == "__main__":
    unittest.main()
