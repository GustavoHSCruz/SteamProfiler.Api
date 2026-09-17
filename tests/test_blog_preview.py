"""The panel's preview: the editor's text drawn the way one() draws a saved
post, with the same cleaning and the same language pick, and nothing written."""
import tempfile
import unittest
from pathlib import Path

import blog
import store

BODY = "![capa](/blog-img/x.png)\n\nUm parágrafo."


class DraftView(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = blog.DB_PATH
        blog.DB_PATH = Path(self.temp.name) / "blog.db"

    def tearDown(self):
        blog.DB_PATH = self.old
        self.temp.cleanup()

    def texts(self, **extra):
        return {"pt": {"title": "Robô não passa da porta", "lede": " ", "body": BODY,
                       "tags": "Segurança, robôs", "slug": ""}, **extra}

    def test_the_shape_post_js_reads(self):
        item = blog.draft_view("pt", "", self.texts(), "pt")
        self.assertEqual(item["title"], "Robô não passa da porta")
        self.assertEqual(item["slug"], "robo-nao-passa-da-porta")
        self.assertEqual(item["tags"], ["segurança", "robôs"])
        self.assertIsNone(item["lede"])
        self.assertTrue(item["translated"])
        self.assertTrue(item["published_at"])
        self.assertIsNone(item["prev"])
        self.assertIsNone(item["next"])

    def test_a_missing_language_falls_back_like_a_reader(self):
        item = blog.draft_view("pt", "", self.texts(), "ru")
        self.assertEqual(item["lang"], "pt")
        self.assertFalse(item["translated"])

    def test_a_half_written_post_is_refused(self):
        with self.assertRaises(store.Rejected):
            blog.draft_view("pt", "", {"pt": {"title": "só título", "body": ""}}, "pt")
        with self.assertRaises(store.Rejected):
            blog.draft_view("en", "", self.texts(), "en")

    def test_the_cover_can_live_on_the_cdn_and_nowhere_else(self):
        cdn = "https://cdn.steamprofiler.org/blog/capa.png"
        self.assertEqual(blog._image(f"![capa]({cdn})\n\ntexto"), (cdn, "capa"))
        self.assertEqual(blog._image("![capa](/blog-img/capa.png)"), ("/blog-img/capa.png", "capa"))
        for other in ("https://example.com/capa.png", "//cdn.steamprofiler.org/capa.png",
                      "https://cdn.steamprofiler.org.example.com/capa.png"):
            self.assertIsNone(blog._image(f"![capa]({other})"), other)

    def test_nothing_is_written(self):
        blog.draft_view("pt", "", self.texts(), "pt")
        self.assertFalse(blog.DB_PATH.exists())


if __name__ == "__main__":
    unittest.main()
