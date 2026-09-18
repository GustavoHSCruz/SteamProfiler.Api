import unittest
from datetime import date, timedelta

import rep


def payload(**over):
    """An established, clean account, with anything overridden per test."""
    profile = {
        "days_since": 3650, "level": 100, "limited": False, "friends": 150,
        "avatar": "https://avatars.example/abc_full.jpg", "custom_url": "x",
        "bio": "hi", "location": "Somewhere", "badge_count": 50,
        "achievements_total": 2000, "groups": 3, "screenshots": 10,
        "reviews": 2, "workshop": 1, "bans": None,
        "items": {k: {"image_large": "x"} for k in ("background", "frame", "avatar", "mini")},
    }
    profile.update(over.pop("profile", {}))
    totals = {"hours": 3000, "owned": 300, "hours_per_day": 0.8, "top_game_share": 20}
    totals.update(over.pop("totals", {}))
    out = {"profile": profile, "totals": totals,
           "library": [{"hours": 10}] * 40}
    out.update(over)
    return out


CLEAN_FRIENDS = {"sampled": 100, "flagged": 0}


class ReputationTest(unittest.TestCase):

    def test_the_weights_are_a_hundred(self):
        self.assertEqual(sum(rep.WEIGHTS.values()), 100)

    def test_a_full_account_is_a_hundred(self):
        got = rep.score(payload(), CLEAN_FRIENDS)
        self.assertEqual(got["score"], 100)
        self.assertEqual(got["known"], 100)
        self.assertIsNone(got["cap"])

    def test_a_week_old_limited_account_is_low(self):
        got = rep.score(payload(
            profile={"days_since": 7, "level": 0, "limited": True, "friends": 0,
                     "avatar": f"https://x/{rep.DEFAULT_AVATAR}_full.jpg",
                     "custom_url": None, "bio": None, "location": None,
                     "badge_count": 0, "achievements_total": 0, "groups": None,
                     "screenshots": 0, "reviews": 0, "workshop": 0, "items": {}},
            totals={"hours": 2, "owned": 1, "hours_per_day": 0.3,
                    "top_game_share": 100},
            library=[{"hours": 2}]), None)
        self.assertLess(got["score"], 25)

    def test_an_unknown_signal_leaves_the_sum_instead_of_counting_zero(self):
        """A private friend list is a privacy setting, not zero friends."""
        hidden = payload(profile={"friends": None})
        got = rep.score(hidden, None)
        self.assertEqual(got["known"], 100 - rep.WEIGHTS["friends"] - rep.WEIGHTS["friend_bans"])
        self.assertEqual(got["score"], 100)
        row = next(s for s in got["signals"] if s["key"] == "friend_bans")
        self.assertIsNone(row["points"])

    def test_too_few_friends_checked_is_unknown(self):
        got = rep.score(payload(), {"sampled": 3, "flagged": 3})
        row = next(s for s in got["signals"] if s["key"] == "friend_bans")
        self.assertIsNone(row["points"])

    def test_a_quarter_of_friends_banned_is_worth_nothing(self):
        got = rep.score(payload(), {"sampled": 100, "flagged": 25})
        row = next(s for s in got["signals"] if s["key"] == "friend_bans")
        self.assertEqual(row["points"], 0)

    def test_a_community_ban_caps_rather_than_subtracts(self):
        got = rep.score(payload(profile={"bans": {"community": True}}), CLEAN_FRIENDS)
        self.assertEqual(got["score"], rep.CAPS["community_ban"])
        self.assertEqual(got["raw"], 100)
        self.assertEqual(got["cap"]["reason"], "community_ban")

    def test_the_lowest_ceiling_wins(self):
        got = rep.score(payload(profile={
            "limited": True,
            "bans": {"economy": "banned", "vac": 1, "days_since": 10}}), CLEAN_FRIENDS)
        self.assertEqual(got["score"], rep.CAPS["trade_banned"])

    def test_an_old_vac_ban_recovers_half_over_ten_years(self):
        fresh = rep.score(payload(profile={"bans": {"vac": 1, "days_since": 400}}))
        old = rep.score(payload(profile={"bans": {"vac": 1, "days_since": 3650}}))
        pts = lambda r: next(s for s in r["signals"] if s["key"] == "bans")["points"]
        self.assertLess(pts(fresh), pts(old))
        self.assertEqual(pts(old), rep.WEIGHTS["bans"] / 2)

    def test_a_recent_ban_caps(self):
        got = rep.score(payload(profile={"bans": {"game": 1, "days_since": 30}}), CLEAN_FRIENDS)
        self.assertEqual(got["cap"]["reason"], "recent_ban")

    def test_an_hour_booster_gets_nothing_for_sustained_use(self):
        got = rep.score(payload(totals={"hours_per_day": 20}))
        row = next(s for s in got["signals"] if s["key"] == "sustained")
        self.assertEqual(row["points"], 0)

    def test_one_game_accounts_lose_variety(self):
        broad = rep.score(payload())
        narrow = rep.score(payload(totals={"top_game_share": 98}))
        pts = lambda r: next(s for s in r["signals"] if s["key"] == "variety")["points"]
        self.assertLess(pts(narrow), pts(broad) / 2)

    def test_an_account_shut_for_years_is_not_sustained_use(self):
        """Ten years old, a thousand hours, nothing played in the last five."""
        shut = (date.today() - timedelta(days=5 * 365 + 30)).isoformat()
        got = rep.score(payload(totals={"hours": 1000},
                                library=[{"hours": 25, "last_played": shut}] * 40))
        row = next(s for s in got["signals"] if s["key"] == "sustained")
        self.assertEqual(row["score"], 0)
        self.assertGreater(row["value"]["idle_days"], 5 * 365)

    def test_a_year_away_costs_nothing(self):
        recent = (date.today() - timedelta(days=200)).isoformat()
        undated = rep.score(payload())
        dated = rep.score(payload(library=[{"hours": 10, "last_played": recent}] * 40))
        pts = lambda r: next(s for s in r["signals"] if s["key"] == "sustained")["score"]
        # The 200 days are taken off the years in use, and nothing else.
        self.assertGreater(pts(dated), pts(undated) - 5)

    def test_the_years_in_use_end_at_the_last_game(self):
        """Opened ten years ago, last played three years ago: seven years in
        use, and two of the three silent ones already fading."""
        last = (date.today() - timedelta(days=3 * 365)).isoformat()
        got = rep.score(payload(library=[{"hours": 75, "last_played": last}] * 40))
        row = next(s for s in got["signals"] if s["key"] == "sustained")
        self.assertLess(row["score"], 60)
        self.assertGreater(row["score"], 30)

    def test_the_level_is_read_as_a_percentile(self):
        """Level 24 is above 97% of Steam, and the signal says so."""
        got = rep.score(payload(profile={"level": 24, "level_percentile": 97.38}))
        row = next(s for s in got["signals"] if s["key"] == "level")
        self.assertEqual(row["score"], 97)

    def test_every_known_signal_has_its_own_score(self):
        got = rep.score(payload(profile={"friends": None}), None)
        for s in got["signals"]:
            if s["points"] is None:
                self.assertIsNone(s["score"])
            else:
                self.assertTrue(0 <= s["score"] <= 100)

    def test_the_default_avatar_is_not_a_chosen_one(self):
        got = rep.score(payload(profile={"avatar": f"https://x/{rep.DEFAULT_AVATAR}_full.jpg"}))
        row = next(s for s in got["signals"] if s["key"] == "profile")
        self.assertNotIn("avatar", row["value"])


if __name__ == "__main__":
    unittest.main()
