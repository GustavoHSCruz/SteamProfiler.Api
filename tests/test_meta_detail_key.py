import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import meta


def detail(appid, name, key):
    """An unfiltered appdetails answer, keyed the way the store keys it."""
    return {str(key): {"success": True, "data": {
        "type": "game", "name": name, "steam_appid": appid, "is_free": False,
        "release_date": {"date": "18 Apr, 2011"}, "genres": [],
    }}}


class DetailKeyTest(unittest.TestCase):
    """The store answered Portal 2 as "323180", one of its DLC, and the
    detail pass wrote Portal 2 down as not on the store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = (meta.DATA_DIR, meta.DB_PATH, meta._ready)
        meta.DATA_DIR = Path(self.temp.name)
        meta.DB_PATH = meta.DATA_DIR / "meta.db"
        meta._ready = False
        meta.init()

    def tearDown(self):
        meta.DATA_DIR, meta.DB_PATH, meta._ready = self.saved
        self.temp.cleanup()

    def row(self, appid):
        with closing(sqlite3.connect(meta.DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT name, exists_on_store, detail_at FROM apps WHERE appid = ?",
                               (appid,)).fetchone()

    def test_answer_keyed_by_a_dlc_is_still_the_game(self):
        with mock.patch.object(meta, "_get", return_value=detail(620, "Portal 2", 323180)):
            self.assertTrue(meta._do_detail(620))
        row = self.row(620)
        self.assertEqual(row["name"], "Portal 2")
        self.assertEqual(row["exists_on_store"], 1)

    def test_answer_about_another_app_says_nothing(self):
        other = detail(999, "Something else", 999)
        with mock.patch.object(meta, "_get", return_value=other):
            self.assertFalse(meta._do_detail(620))
        row = self.row(620)
        self.assertTrue(row is None or row["exists_on_store"] is None)

    def test_a_real_no_is_still_a_no(self):
        with mock.patch.object(meta, "_get", return_value={"620": {"success": False}}):
            self.assertTrue(meta._do_detail(620))
        self.assertEqual(self.row(620)["exists_on_store"], 0)

    def test_entry_prefers_the_key(self):
        body = {"620": {"success": True, "data": {"steam_appid": 620}}}
        self.assertIs(meta._entry_for(body, 620), body["620"])
        self.assertIsNone(meta._entry_for(None, 620))


if __name__ == "__main__":
    unittest.main()
