import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import meta


class SearchCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = meta.DATA_DIR
        self.old_db_path = meta.DB_PATH
        self.old_ready = meta._ready
        meta.DATA_DIR = Path(self.temp.name)
        meta.DB_PATH = meta.DATA_DIR / "meta.db"
        meta._ready = False
        meta.init()

    def tearDown(self):
        meta.DATA_DIR = self.old_data_dir
        meta.DB_PATH = self.old_db_path
        meta._ready = self.old_ready
        self.temp.cleanup()

    @staticmethod
    def page(apps, more=False, cursor=None):
        return {
            "apps": apps,
            "have_more_results": more,
            "last_appid": cursor,
        }

    def test_syncs_pages_and_searches_unvisited_punctuated_names(self):
        pages = {
            0: self.page([
                {"appid": 10, "name": "R.E.P.O.", "last_modified": 1,
                 "price_change_number": 2},
                {"appid": 20, "name": "A Game Never Visited"},
            ], more=True, cursor=20),
            20: self.page([
                {"appid": 30, "name": "EA SPORTS FC™ 26"},
            ]),
        }

        result = meta.sync_search_catalog("secret", pages.__getitem__)

        self.assertEqual(result["games"], 3)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(meta.search_games("repo")[0]["appid"], 10)
        self.assertEqual(meta.search_games("repo")[0]["name"], "R.E.P.O.")
        self.assertEqual(meta.search_games("r.e")[0]["appid"], 10)
        self.assertEqual(meta.search_games("game never")[0]["appid"], 20)
        self.assertEqual(meta.search_games("EA Sports FC 26")[0]["appid"], 30)
        self.assertEqual(meta.search_games("30")[0]["name"], "EA SPORTS FC™ 26")
        self.assertEqual(meta.stats()["search_games"], 3)

    def test_failed_pagination_keeps_last_good_snapshot(self):
        meta.sync_search_catalog("secret", lambda _: self.page([
            {"appid": 10, "name": "Last Good Game"},
        ]))

        def broken(cursor):
            if cursor == 0:
                return self.page([{"appid": 20, "name": "Partial Game"}],
                                 more=True, cursor=20)
            raise TimeoutError("simulated")

        with self.assertRaises(TimeoutError):
            meta.sync_search_catalog("secret", broken)

        self.assertEqual(meta.search_games("last good")[0]["appid"], 10)
        self.assertEqual(meta.search_games("partial"), [])

    def test_missing_game_is_unlisted_without_being_deleted(self):
        meta.sync_search_catalog("secret", lambda _: self.page([
            {"appid": 10, "name": "Still Here"},
            {"appid": 20, "name": "Gone From List"},
        ]))
        meta.sync_search_catalog("secret", lambda _: self.page([
            {"appid": 10, "name": "Still Here"},
        ]))

        self.assertEqual(meta.search_games("gone from"), [])
        with closing(sqlite3.connect(meta.DB_PATH)) as con, con:
            self.assertEqual(
                con.execute("SELECT listed FROM search_apps WHERE appid = 20").fetchone()[0],
                0,
            )

    def test_locally_learned_delisted_game_remains_a_fallback(self):
        with closing(sqlite3.connect(meta.DB_PATH)) as con, con:
            con.execute(
                "INSERT INTO apps (appid, name, exists_on_store) VALUES (?, ?, ?)",
                (99, "Old Delisted Game", 1),
            )
        self.assertEqual(meta.search_games("old delisted")[0]["appid"], 99)

    def test_rejects_empty_key_and_non_progressing_page(self):
        with self.assertRaisesRegex(ValueError, "STEAM_API_KEY"):
            meta.sync_search_catalog("", lambda _: self.page([]))
        with self.assertRaisesRegex(ValueError, "non-progressing"):
            meta.sync_search_catalog("secret", lambda _: self.page(
                [{"appid": 10, "name": "Loop"}], more=True, cursor=0,
            ))


if __name__ == "__main__":
    unittest.main()
