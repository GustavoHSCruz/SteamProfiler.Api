import tempfile
import unittest
from pathlib import Path

import fx


class RateTest(unittest.TestCase):
    """The source is never called here. What is worth testing is what happens
    to the answer after it arrives: which fields are trusted, what an old rate
    does, and what a failed read leaves behind."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = fx.DATA_DIR
        self.old_db_path = fx.DB_PATH
        self.old_ready = fx._ready
        self.old_get = fx._get
        fx.DATA_DIR = Path(self.temp.name)
        fx.DB_PATH = fx.DATA_DIR / "meta.db"
        fx._ready = False
        fx._next_try = 0.0
        fx._forget()
        fx.init()

    def tearDown(self):
        fx.DATA_DIR = self.old_data_dir
        fx.DB_PATH = self.old_db_path
        fx._ready = self.old_ready
        fx._get = self.old_get
        fx._next_try = 0.0
        fx._forget()
        self.temp.cleanup()

    @staticmethod
    def answer(**rates):
        return {"date": "2026-08-30", "usd": {k.lower(): v for k, v in rates.items()}}

    def test_reads_the_currencies_the_site_speaks(self):
        fx._get = lambda url: self.answer(BRL=5.19, RUB=86.02, EUR=0.86)
        self.assertTrue(fx.refresh(force=True))
        got = fx.quote()
        self.assertEqual(got["at"], "2026-08-30")
        self.assertEqual(got["rates"], {"BRL": 5.19, "RUB": 86.02})

    def test_a_source_that_answers_nothing_useful_falls_through(self):
        """A shape that changed is likelier than the real ceasing to exist, so
        an answer without any of the wanted currencies is a failed source and
        not an empty day."""
        seen = []

        def get(url):
            seen.append(url)
            return self.answer(EUR=0.86) if len(seen) == 1 else self.answer(BRL=5.19)

        fx._get = get
        self.assertTrue(fx.refresh(force=True))
        self.assertEqual(len(seen), 2)
        self.assertEqual(fx.quote()["rates"], {"BRL": 5.19})

    def test_a_rate_that_is_not_a_rate_is_refused(self):
        fx._get = lambda url: self.answer(BRL="5,19", RUB=0)
        self.assertFalse(fx.refresh(force=True))
        self.assertIsNone(fx.quote())

    def test_an_old_rate_leaves_the_page_rather_than_ageing_on_it(self):
        fx._get = lambda url: self.answer(BRL=5.19, RUB=86.02)
        fx.refresh(force=True)
        with fx._connect() as con:
            con.execute("UPDATE fx_rates SET seen_at = '2020-01-01T00:00:00+00:00'")
        fx._forget()
        self.assertIsNone(fx.quote())

    def test_a_source_that_says_nothing_leaves_yesterdays_rate_alone(self):
        """An approximation from yesterday still says what the page needs to
        say. An empty panel says nothing at all."""
        fx._get = lambda url: self.answer(BRL=5.19, RUB=86.02)
        fx.refresh(force=True)
        fx._get = lambda url: None
        self.assertFalse(fx.refresh(force=True))
        self.assertEqual(fx.quote()["rates"], {"BRL": 5.19, "RUB": 86.02})

    def test_a_fresh_rate_is_not_fetched_again(self):
        calls = []
        fx._get = lambda url: calls.append(url) or self.answer(BRL=5.19, RUB=86.02)
        self.assertTrue(fx.refresh(force=True))
        self.assertFalse(fx.refresh())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
