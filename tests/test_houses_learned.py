"""What houses.py learns outside the weekly walk.

The walk is half the catalogue - `request=all` ends at page 86 - so everything
in these tests is about the other half: a company that exists only because one
app was learned one at a time, and the rebuild that must not throw it away.
"""
import tempfile
import unittest
from pathlib import Path

import houses


class LearnedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_db = houses.DATA_DIR, houses.DB_PATH
        self.old_ready, self.old_gap = houses._ready, houses.PAGE_GAP
        houses.DATA_DIR = Path(self.temp.name)
        houses.DB_PATH = houses.DATA_DIR / "houses.db"
        houses._ready = False
        houses.PAGE_GAP = 0
        houses.init()

    def tearDown(self):
        houses.DATA_DIR, houses.DB_PATH = self.old_dir, self.old_db
        houses._ready, houses.PAGE_GAP = self.old_ready, self.old_gap
        self.temp.cleanup()

    def test_a_list_is_not_split_on_commas_again(self):
        # The storefront hands over a list and SteamSpy a joined string. Run
        # the comma rule on the list and `CAPCOM Co., Ltd.` comes apart; skip
        # it on the string and three studios become one.
        self.assertEqual(houses.names_from(["Horny Capybara Studio"]),
                         ["Horny Capybara Studio"])
        self.assertEqual(houses.names_from(["CAPCOM Co., Ltd."]), ["CAPCOM Co., Ltd."])
        self.assertEqual(houses.names_from("Treyarch, Raven Software, Beenox"),
                         ["Treyarch", "Raven Software", "Beenox"])
        self.assertEqual(houses.names_from("CAPCOM Co., Ltd."), ["CAPCOM Co., Ltd."])
        self.assertEqual(houses.names_from(None), [])

    def test_a_list_does_not_arrive_as_its_own_repr(self):
        # The bug this was written after: the publisher went into the index
        # under `['Horny Capybara Studio']`, brackets and quotes included.
        houses.learn(4355490, "Sex-Pop Demon Hunters",
                     publishers=["Horny Capybara Studio"],
                     developers=["Sweet Buns Games"])
        got = houses.index("publisher", "capybara")["houses"]
        self.assertEqual([h["name"] for h in got], ["Horny Capybara Studio"])
        self.assertEqual(got[0]["slug"], "horny-capybara-studio")

    def test_one_learned_app_is_a_company_that_can_be_opened(self):
        houses.learn(4355490, "Sex-Pop Demon Hunters",
                     publishers=["Horny Capybara Studio"],
                     developers=["Sweet Buns Games"])
        shelf = houses.shelf("publisher", "horny-capybara-studio")
        self.assertEqual(shelf["games"], 1)
        # The only game it is known to have is also the picture that stands
        # for it, because there is nothing else to choose from.
        self.assertEqual(shelf["art"], 4355490)
        self.assertEqual([a["appid"] for a in shelf["apps"]], [4355490])

    def _walk(self, publisher="Editora B"):
        """A walk big enough to pass the guard that rejects a bad day."""
        pages = {
            0: {str(i): {"appid": i, "name": f"Jogo {i}", "developer": "Estudio A",
                         "publisher": publisher, "positive": i, "negative": 0}
                for i in range(1, 1200)},
            1: {"2000": {"appid": 2000, "name": "Jogo 2000", "developer": "Estudio C",
                         "publisher": publisher, "positive": 1, "negative": 0}},
            2: {},
        }
        houses._fetch = lambda page, tries=3: pages.get(page)

    def test_the_weekly_rebuild_does_not_forget_what_was_learned(self):
        # refresh() drops the tables and swaps fresh ones in. Without the
        # merge that would be a weekly amnesia over half the catalogue.
        houses.learn(4355490, "Sex-Pop Demon Hunters",
                     publishers=["Horny Capybara Studio"])
        self._walk()
        result = houses.refresh(force=True)
        self.assertNotIn("failed", result)
        got = houses.index("publisher", "capybara")["houses"]
        self.assertEqual([h["name"] for h in got], ["Horny Capybara Studio"])
        self.assertEqual(houses.shelf("publisher", "horny-capybara-studio")["games"], 1)

    def test_the_walk_keeps_the_face_it_chose(self):
        # A learned row knows one app. The walk picks the picture from review
        # counts across everything a company shipped, and that considered
        # choice must not be replaced by the arbitrary one.
        self._walk()
        houses.refresh(force=True)
        before = houses.shelf("publisher", "editora-b")["art"]
        houses.learn(999999, "Jogo tardio", publishers=["Editora B"])
        houses.refresh(force=True)
        self.assertEqual(houses.shelf("publisher", "editora-b")["art"], before)

    def test_the_backfill_skips_what_the_walk_already_answered(self):
        asked = []

        def detail(appid, tries=2):
            asked.append(appid)
            return {"appid": appid, "name": f"Jogo {appid}",
                    "publisher": "Editora Z", "developer": "Estudio Z"}

        self._walk()
        houses.refresh(force=True)
        houses._detail = detail
        houses.DETAIL_GAP = 0
        # 1 and 2000 came from the walk; only the third is unanswered.
        got = houses.backfill(budget=10, catalogue=lambda a, l: [
            x for x in (1, 2000, 4355490) if x > a][:l])
        self.assertEqual(asked, [4355490])
        self.assertEqual(got["found"], 1)
        # Off the end of the catalogue, so the lap closed and the next one
        # starts at the beginning rather than running off into nothing.
        self.assertEqual(got["cursor"], 0)
        self.assertEqual(got["laps"], 1)

    def test_the_backfill_resumes_where_the_budget_ran_out(self):
        houses._detail = lambda appid, tries=2: {
            "appid": appid, "name": f"Jogo {appid}", "publisher": "Editora Z"}
        houses.DETAIL_GAP = 0
        catalogue = lambda a, l: [x for x in (10, 20, 30, 40) if x > a][:l]  # noqa: E731
        first = houses.backfill(budget=2, catalogue=catalogue)
        self.assertEqual(first["asked"], 2)
        second = houses.backfill(budget=2, catalogue=catalogue)
        self.assertEqual(second["asked"], 2)
        # Four apps asked about once each, not the first two asked twice.
        self.assertEqual(houses.learned_state()["apps"], 4)


if __name__ == "__main__":
    unittest.main()
