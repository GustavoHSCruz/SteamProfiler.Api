import unittest

import community
import inv


def card(appid, name, classid, border="cardborder_0", item_class="item_class_2",
         icon="ICON"):
    return {
        "classid": str(classid), "instanceid": "0",
        "market_hash_name": f"{appid}-{name}",
        "market_fee_app": appid,
        "icon_url": icon,
        "tags": [{"category": "Game", "internal_name": f"app_{appid}"},
                 {"category": "item_class", "internal_name": item_class},
                 {"category": "cardborder", "internal_name": border}],
    }


def asset(classid, amount=1):
    return {"classid": str(classid), "instanceid": "0", "amount": str(amount)}


def answer(assets, descriptions, total=None):
    return {"success": 1, "assets": assets, "descriptions": descriptions,
            "total_inventory_count": total if total is not None else len(assets)}


class InventoryTest(unittest.TestCase):
    """Steam is never called here. What is worth testing is which items survive
    the filter and which of the four states a read lands in, and both of those
    are decided after the answer arrives."""

    def setUp(self):
        self.old_fetch = inv._fetch
        inv.forget()
        community.reset()
        self.addCleanup(self.restore)

    def restore(self):
        inv._fetch = self.old_fetch
        inv.forget()
        community.reset()

    def serve(self, body, status=200):
        inv._fetch = lambda steamid: (status, body)

    def test_it_counts_copies_rather_than_rows(self):
        """`amount` is per asset row and one card can be spread over several of
        them, so four of a card is four whether it arrives as one row or two."""
        self.serve(answer([asset(1, 4), asset(2, 1)],
                          [card(730, "FBI", 1), card(730, "IDF", 2)]))
        got = inv.of("1", wait=0)
        self.assertEqual(got["state"], "ok")
        self.assertEqual(got["held"][730], {"730-FBI": 4, "730-IDF": 1})
        self.assertEqual(got["cards"], 5)
        self.assertEqual(got["dupes"], 3)

    def test_a_foil_is_not_counted_against_the_normal_set(self):
        """A foil is a second collection with its own prices and its own badge.
        Counting one here would make the set cards.py priced look nearer to done
        than it is."""
        self.serve(answer([asset(1), asset(2)],
                          [card(620, "GLaDOS", 1),
                           card(620, "Wheatley", 2, border="cardborder_1")]))
        got = inv.of("1", wait=0)
        self.assertEqual(got["held"][620], {"620-GLaDOS": 1})

    def test_an_emoticon_is_not_a_card(self):
        self.serve(answer([asset(1), asset(2)],
                          [card(620, "GLaDOS", 1),
                           card(620, ":wheatley:", 2, item_class="item_class_4")]))
        self.assertEqual(inv.of("1", wait=0)["held"][620], {"620-GLaDOS": 1})

    def test_the_game_tag_stands_in_when_the_fee_app_is_missing(self):
        desc = card(620, "GLaDOS", 1)
        del desc["market_fee_app"]
        self.serve(answer([asset(1)], [desc]))
        self.assertEqual(inv.of("1", wait=0)["held"], {620: {"620-GLaDOS": 1}})

    def test_an_asset_with_no_description_is_dropped_rather_than_guessed_at(self):
        """Steam sends the two lists separately. A card whose description never
        arrived cannot be identified, and inventing an appid for it would put a
        card against the wrong game's set."""
        self.serve(answer([asset(1), asset(99)], [card(620, "GLaDOS", 1)]))
        got = inv.of("1", wait=0)
        self.assertEqual(got["held"], {620: {"620-GLaDOS": 1}})
        self.assertEqual(got["cards"], 1)

    def test_a_private_inventory_is_an_answer_and_is_remembered(self):
        """403 is a fact, not a failure. Asking again on the next page load
        would spend a slot on the shared community budget to be told the same
        no."""
        calls = []

        def once(steamid):
            calls.append(steamid)
            return 403, None

        inv._fetch = once
        self.assertEqual(inv.of("1", wait=0)["state"], "private")
        self.assertEqual(inv.of("1", wait=0)["state"], "private")
        self.assertEqual(len(calls), 1)

    def test_more_items_than_one_read_holds_says_truncated(self):
        """The cards that were read are all real; what the totals under them
        must not do is claim to be the whole collection."""
        self.serve(answer([asset(1)], [card(620, "GLaDOS", 1)], total=9000))
        got = inv.of("1", wait=0)
        self.assertEqual(got["state"], "truncated")
        self.assertEqual(got["count"], 9000)
        self.assertEqual(got["read"], 1)

    def test_a_host_that_says_nothing_is_not_cached(self):
        """Unlike a 403, "no answer" is not an answer, so the next page open
        tries again instead of holding an empty panel for fifteen minutes."""
        inv._fetch = lambda steamid: (500, None)
        self.assertEqual(inv.of("1", wait=0)["state"], "unknown")
        self.assertIsNone(inv.known("1"))

    def test_a_cooling_host_is_not_asked_at_all(self):
        """Asking during a refusal is what renews the refusal."""
        asked = []
        inv._fetch = lambda steamid: (asked.append(steamid), (200, answer([], [])))[1]
        community.note_429()
        self.assertEqual(inv.of("1", wait=0)["state"], "unknown")
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
