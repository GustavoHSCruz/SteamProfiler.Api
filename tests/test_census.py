"""The screen count: what it reads off a path, and what it refuses to record.

Three promises are tested here rather than three behaviours, because the
behaviour is the easy half.

    the map        census.py holds a copy of what nginx.conf routes. Nothing
                   makes the two agree on their own, so a renamed route is a
                   screen that silently reads zero forever. These cases are
                   the addresses the site actually serves, written out, so
                   that a rename fails here instead of on the panel.

    the rule       A view is a change of screen. A reload is the same screen
                   and is not a second view; leaving and coming back is two
                   changes and is two views. That is what the owner asked for
                   and it is a rule about memory, not about a person.

    the shape      `screens` has no visitor column and no profile column, and
                   neither does `screen_games`. If a later change adds one,
                   this fails, and it should: the whole point of counting
                   screens instead of paths is that the profile in
                   /u/<who>/cards never reaches the disk.
"""
import tempfile
import unittest
from pathlib import Path

import census

BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


class ScreenMapTest(unittest.TestCase):
    def test_the_addresses_the_site_serves(self):
        for path, screen in (
            ("/", "site.home"),
            ("/u/gordziilla", "u.dash"),
            ("/u/gordziilla/", "u.dash"),
            ("/u/gordziilla/440", "u.game"),
            ("/u/gordziilla/vs/outro", "u.versus"),
            ("/u/gordziilla/backlog", "u.backlog"),
            ("/u/gordziilla/cards", "u.cards"),
            ("/u/gordziilla/year/2019", "u.year"),
            ("/u/gordziilla/franchises", "u.franchises"),
            ("/u/gordziilla/franchises/half-life", "u.franchise"),
            ("/u/gordziilla/publishers", "u.publishers"),
            ("/u/gordziilla/publishers/valve", "u.publisher"),
            ("/u/gordziilla/developers", "u.developers"),
            ("/u/gordziilla/developers/facepunch", "u.developer"),
            ("/u/gordziilla/deck", "u.deck"),
            ("/u/gordziilla/embed", "u.embed"),
            ("/u/gordziilla/ids", "u.ids"),
            ("/g/440", "cat.game"),
            ("/franchises", "cat.franchises"),
            ("/franchises/half-life", "cat.franchise"),
            ("/publishers", "cat.publishers"),
            ("/publishers/valve", "cat.publisher"),
            ("/developers", "cat.developers"),
            ("/developers/facepunch", "cat.developer"),
            ("/blog", "blog.index"),
            ("/blog/a-post", "blog.post"),
            ("/blog/a-post/o-titulo-dele", "blog.post"),
            ("/news", "site.news"),
            ("/news/9876543", "site.news_post"),
            ("/about", "site.about"),
            ("/privacy", "site.privacy"),
            ("/privacy/history", "site.privacy_log"),
            ("/status", "site.status"),
            ("/feedback", "site.feedback"),
            ("/support", "site.support"),
            ("/extension", "site.extension"),
            ("/translate", "site.translate"),
            ("/appeal", "site.appeal"),
            ("/appeal/sent", "site.appeal_sent"),
        ):
            with self.subTest(path=path):
                self.assertEqual(census.screen_of(path)[0], screen)

    def test_the_two_game_screens_keep_the_appid(self):
        self.assertEqual(census.screen_of("/u/gordziilla/440"),
                         ("u.game", "440", "440"))
        self.assertEqual(census.screen_of("/g/440"), ("cat.game", "440", "440"))

    def test_a_shelf_is_told_apart_but_never_written_down(self):
        """The middle answer is what reaches the disk and the last one is what
        tells two openings of one screen apart. A franchise has the second and
        not the first: reading half-life and then portal is two views, and
        neither slug is recorded anywhere."""
        screen, appid, tail = census.screen_of("/u/gordziilla/franchises/half-life")
        self.assertEqual((screen, appid, tail), ("u.franchise", None, "half-life"))
        # And the profile is in neither, on any screen that has one.
        for path in ("/u/gordziilla", "/u/gordziilla/cards",
                     "/u/gordziilla/vs/outro", "/u/gordziilla/franchises/portal"):
            with self.subTest(path=path):
                self.assertNotIn("gordziilla", str(census.screen_of(path)))

    def test_what_is_not_a_screen(self):
        """Assets, the api, the art, a scanner's shopping list. A page that is
        not on the map is not counted at all, deliberately: an "other" bucket
        on a site that answers the open internet is a bucket of /wp-admin."""
        for path in ("/style.css", "/lib.js", "/fonts/inter.woff2",
                     "/art/440.jpg", "/assets/index-a1b2c3.js", "/api/profile",
                     "/favicon.svg", "/sitemap.xml", "/robots.txt",
                     "/wp-admin", "/.env", "/u/gordziilla/nao-existe", ""):
            with self.subTest(path=path):
                self.assertEqual(census.screen_of(path), (None, None, None))


class ScreenCountTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = (census.DATA_DIR, census.DB_PATH)
        census.DATA_DIR = Path(self.temp.name)
        census.DB_PATH = census.DATA_DIR / "census.db"
        census._slots.clear()
        census._subjects.clear()
        census.init()

    def tearDown(self):
        census.DATA_DIR, census.DB_PATH = self.saved
        census._slots.clear()
        census._subjects.clear()
        self.temp.cleanup()

    def visit(self, address, path):
        """A document and the assets that follow it, the way the gate sees a
        page being opened. The assets are what make the class settle on
        `visitor`, and they are not screens."""
        census.note(address, path=path, ua=BROWSER, country="BR", region="SP")
        for asset in ("/style.css", "/lib.js", "/fonts/inter.woff2"):
            census.note(address, path=asset, ua=BROWSER, country="BR", region="SP")

    def counts(self):
        return {row["screen"]: row["hits"] for row in census.report()["screens"]}

    def test_a_reload_is_not_a_second_view(self):
        self.visit("198.51.100.7", "/u/gordziilla")
        self.visit("198.51.100.7", "/u/gordziilla")
        self.visit("198.51.100.7", "/u/gordziilla")
        census.flush()
        self.assertEqual(self.counts(), {"u.dash": 1})

    def test_leaving_and_coming_back_is_two(self):
        self.visit("198.51.100.7", "/u/gordziilla")
        self.visit("198.51.100.7", "/u/gordziilla/cards")
        self.visit("198.51.100.7", "/u/gordziilla")
        census.flush()
        self.assertEqual(self.counts(), {"u.dash": 2, "u.cards": 1})

    def test_two_shelves_in_a_row_are_two_views(self):
        """The screen is the same name; the shelf is not. Without the second
        half of the comparison this counts once, which is the bug this case
        was written for."""
        self.visit("198.51.100.7", "/franchises/half-life")
        self.visit("198.51.100.7", "/franchises/portal")
        self.visit("198.51.100.7", "/franchises/portal")
        census.flush()
        self.assertEqual(self.counts(), {"cat.franchise": 2})

    def test_the_game_screens_count_the_game(self):
        self.visit("198.51.100.7", "/u/gordziilla/440")
        self.visit("198.51.100.7", "/g/440")
        self.visit("198.51.100.7", "/g/570")
        census.flush()
        games = {(g["screen"], g["appid"]): g["hits"]
                 for g in census.report()["screen_games"]}
        self.assertEqual(games, {("u.game", "440"): 1, ("cat.game", "440"): 1,
                                 ("cat.game", "570"): 1})

    def test_a_crawler_is_counted_apart_from_a_reader(self):
        self.visit("198.51.100.7", "/blog")
        census.note("203.0.113.9", path="/blog", ua="ClaudeBot/1.0")
        census.flush()
        blog = next(r for r in census.report()["screens"]
                    if r["screen"] == "blog.index")
        self.assertEqual(blog["by_class"], {"visitor": 1, "ai": 1})

    def test_neither_table_has_anywhere_to_put_a_person(self):
        with census._connect() as con:
            for table in ("screens", "screen_games"):
                columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                self.assertNotIn("hash", columns)
                self.assertNotIn("steamid", columns)
                self.assertNotIn("country", columns)
                self.assertNotIn("region", columns)


if __name__ == "__main__":
    unittest.main()
