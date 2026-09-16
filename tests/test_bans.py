import tempfile
import unittest
from pathlib import Path
import bans
import store


class HashBanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = bans.DATA_DIR, bans.DB_PATH, store._salt
        bans.DATA_DIR = Path(self.temp.name)
        bans.DB_PATH = bans.DATA_DIR / "bans.db"
        store._salt = "ban-test-salt"
        bans._active.clear()
        bans._probe.clear()
        bans.init()

    def tearDown(self):
        bans.DATA_DIR, bans.DB_PATH, store._salt = self.saved
        bans._active.clear()
        bans._probe.clear()
        self.temp.cleanup()

    def test_owner_can_ban_the_identifier_kept_by_security_history(self):
        actor = "a" * 64
        stamp = bans.ban_hash(actor, path="/g/440")
        row = bans.by_hash(actor)
        self.assertGreater(stamp, 0)
        self.assertTrue(row["active"])
        self.assertEqual(row["reason"], "admin")
        self.assertEqual(row["paths"], ["/g/440"])

    def test_hash_ban_never_accepts_an_address_or_arbitrary_text(self):
        for value in ("198.51.100.7", "", "x" * 65, "' OR 1=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bans.ban_hash(value)


if __name__ == "__main__":
    unittest.main()
