import os
import unittest

os.environ.setdefault("STEAM_API_KEY", "test")
os.environ.setdefault("STEAM_ID", "76561198000000000")

import fetch


def item(**over):
    """One Workshop row, in the shape a real answer has. Checked against a live
    creator's five published items before being written down here."""
    row = {
        "publishedfileid": "3794238528",
        "title": "bitumen",
        "preview_url": "https://images.steamusercontent.com/ugc/171/8224.jpg",
        "consumer_appid": 4000,
        "subscriptions": 12,
        "favorited": 3,
        "views": 40,
        "vote_data": {"votes_up": 9, "votes_down": 1},
        "time_created": 1750000000,
        "time_updated": 1750500000,
        "short_description": "a lump of it",
    }
    row.update(over)
    return row


class WorkshopTest(unittest.TestCase):
    """Steam is never called here. The reason this file exists is that the
    owner of the site has published nothing, so the empty answer is the only
    one production would ever have exercised."""

    def setUp(self):
        self.old = fetch.get_json
        self.addCleanup(lambda: setattr(fetch, "get_json", self.old))

    def serve(self, payload):
        fetch.get_json = lambda path, **kw: payload

    def test_a_profile_that_published_nothing_says_so_without_a_row(self):
        self.serve({"total": 0})
        got = fetch.build_workshop()
        self.assertEqual(got["total"], 0)
        self.assertEqual(got["items"], [])
        self.assertFalse(got["truncated"])

    def test_it_reads_a_real_row(self):
        self.serve({"total": 1, "publishedfiledetails": [item()]})
        row = fetch.build_workshop()["items"][0]
        self.assertEqual(row["id"], "3794238528")
        self.assertEqual(row["title"], "bitumen")
        self.assertEqual(row["appid"], 4000)
        self.assertEqual(row["votes"], {"up": 9, "down": 1})
        self.assertEqual(row["created"], "2025-06-15")

    def test_a_row_with_no_id_or_no_title_is_dropped_rather_than_half_built(self):
        """There is nothing to link to and nothing to call it. A blank row on
        the page would be worse than one fewer row."""
        self.serve({"total": 3, "publishedfiledetails": [
            item(), item(publishedfileid=""), item(title=None)]})
        got = fetch.build_workshop()
        self.assertEqual(len(got["items"]), 1)
        # The count Steam gave still stands, and the gap is admitted.
        self.assertEqual(got["total"], 3)
        self.assertTrue(got["truncated"])

    def test_a_field_in_an_unexpected_shape_goes_missing_instead_of_raising(self):
        """This is the one builder here whose full answer could not be checked
        against the owner's own profile, so every optional field is read
        defensively and a surprise costs one field, not the panel."""
        self.serve({"total": 1, "publishedfiledetails": [
            item(views="lots", time_created={"nope": 1}, vote_data=None,
                 preview_url=None)]})
        row = fetch.build_workshop()["items"][0]
        self.assertIsNone(row["views"])
        self.assertIsNone(row["created"])
        self.assertIsNone(row["votes"])
        self.assertIsNone(row["preview"])
        self.assertEqual(row["title"], "bitumen")

    def test_nothing_at_all_is_an_empty_panel_and_not_a_crash(self):
        """required=False answers None when Steam refuses, and a profile build
        must survive that."""
        self.serve(None)
        got = fetch.build_workshop()
        self.assertEqual((got["total"], got["items"]), (0, []))


if __name__ == "__main__":
    unittest.main()
