"""embed.py - the pictures that leave the site.

Four things would break quietly here, and each of them breaks somewhere nobody
can fix it: in a README, in a forum post, on somebody else's blog.

    * a file that is not well-formed XML draws as nothing at all
    * a file that references anything outside itself draws as a broken box on
      GitHub, which proxies images, and refuses to become a PNG in the browser
    * a badge narrower than its own text clips it
    * a query string with a typo in it must still answer with a picture
"""
import re
import unittest
import xml.etree.ElementTree as ET

import embed
import sign


PROFILE = {
    "generated_at": "2026-09-09T14:12:00+00:00",
    "profile": {"persona": "Gordziilla", "level": 47, "member_since": "2011-03-18",
                "avatar": "https://avatars.steamstatic.com/abc_full.jpg",
                "achievements_total": 3412, "badge_count": 61},
    "totals": {"hours": 4183, "owned": 351, "played": 212, "never_played": 139,
               "hours_per_day": 2.1},
    "platform": {"windows_hours": 3120, "linux_hours": 940, "mac_hours": 12,
                 "deck_hours": 111},
    "genres": [{"id": 1, "name": "Ação", "hours": 2450.5},
               {"id": 2, "name": "Simulação", "hours": 1288.0}],
    "library": [
        {"appid": 730, "name": "Counter-Strike 2", "hours": 1284.4,
         "last_played": "2026-06-01", "minutes_2weeks": 320},
        {"appid": 107410, "name": "Arma 3", "hours": 963.2,
         "last_played": "2025-06-01", "minutes_2weeks": 60},
        {"appid": 271590, "name": "Grand Theft Auto V Legacy", "hours": 412.8,
         "last_played": "2024-06-01", "minutes_2weeks": 0},
    ],
    "now": {"playing": "Counter-Strike 2"},
}
# The second profile the versus card needs: a smaller library that overlaps the
# first one in two games and in nothing else.
RIVAL = {
    "generated_at": "2026-09-09T14:12:00+00:00",
    "profile": {"persona": "Ana Beatriz", "level": 22, "member_since": "2016-07-02",
                "avatar": "https://avatars.steamstatic.com/def_full.jpg",
                "achievements_total": 901, "badge_count": 14},
    "totals": {"hours": 1620, "owned": 128, "played": 88, "never_played": 40,
               "hours_per_day": 0.9},
    "platform": {"windows_hours": 1500, "linux_hours": 60, "mac_hours": 0,
                 "deck_hours": 320},
    "genres": [],
    "library": [
        {"appid": 730, "name": "Counter-Strike 2", "hours": 410.0},
        {"appid": 107410, "name": "Arma 3", "hours": 12.0},
        {"appid": 413150, "name": "Stardew Valley", "hours": 300.0},
    ],
    "now": {},
}

EMPTY = {"generated_at": "2026-09-09T14:12:00+00:00",
         "profile": {"persona": "ninguém"}, "totals": {}, "platform": {},
         "genres": [], "library": [], "now": {}}


def options(kind, **kw):
    return embed.options(kind, lambda name: str(kw.get(name, "")))


class EmbedTest(unittest.TestCase):
    def setUp(self):
        # No trip to Steam and no disk in a unit test. A missing picture is a
        # case the renderers already have to handle, so None is the honest
        # stand-in and it exercises the fallback at the same time.
        self.held = (embed.art.thumb, embed.art.avatar, embed.art.backdrop)
        embed.art.thumb = lambda appid: None
        embed.art.avatar = lambda url: None
        embed.art.backdrop = lambda url: None

    def tearDown(self):
        embed.art.thumb, embed.art.avatar, embed.art.backdrop = self.held

    def every_svg(self):
        """One of everything the site can offer, as (name, svg)."""
        for style in embed.STYLES:
            for show in embed.SHOWS:
                for theme in embed.THEMES:
                    o = options("bars", style=style, show=show, theme=theme,
                                lang="pt", n="4")
                    yield f"bars {style} {show} {theme}", embed.bars(PROFILE, o)
        for preset in embed.PRESETS:
            for theme in embed.THEMES:
                o = options("banner", preset=preset, theme=theme, lang="en")
                yield f"banner {preset} {theme}", embed.banner(PROFILE, o)
        for metric in embed.METRICS:
            for style in embed.BADGE_STYLES:
                o = options("badge", metric=metric, style=style, logo="1", lang="ru")
                yield f"badge {metric} {style}", embed.badge(PROFILE, o)
        for preset in embed.ARTWORKS:
            for bg in embed.BACKDROPS:
                for theme in embed.THEMES:
                    o = options("artwork", preset=preset, bg=bg, theme=theme,
                                lang="pt", sign="Gordziilla", games="4")
                    yield f"artwork {preset} {bg} {theme}", embed.artwork(PROFILE, o)
        for theme in embed.THEMES:
            o = options("versus", theme=theme, lang="en",
                        rows="hours,games,top,now", games="3")
            yield f"versus {theme}", embed.versus(PROFILE, RIVAL, o)

    def test_every_variant_is_well_formed(self):
        for name, svg in self.every_svg():
            with self.subTest(name):
                ET.fromstring(svg)

    def test_nothing_is_fetched_from_outside_the_file(self):
        # The only URL allowed in one of these is the XML namespace. A data URI
        # is not a fetch; anything else is, and the picture would be a hole.
        for name, svg in self.every_svg():
            with self.subTest(name):
                for start in ('href="http', 'src="http', "@import", "<script"):
                    self.assertNotIn(start, svg)
                self.assertEqual(svg.count("http"), 1, "only the namespace")

    def test_an_empty_library_still_draws(self):
        for kind, draw in (("bars", embed.bars), ("banner", embed.banner),
                           ("badge", embed.badge), ("artwork", embed.artwork)):
            with self.subTest(kind):
                ET.fromstring(draw(EMPTY, options(kind, lang="pt")))
        self.assertIn("steamprofiler.org", embed.text_bars(EMPTY, options("text")))
        # Two profiles with nothing in either of them: every split bar on the
        # card is a pair of zeros, which must be an empty track and not a
        # division by nothing.
        ET.fromstring(embed.versus(EMPTY, EMPTY, options("versus", lang="pt")))

    def test_a_badge_is_wider_than_its_own_text(self):
        for metric in embed.METRICS:
            with self.subTest(metric):
                o = options("badge", metric=metric, logo="1")
                svg = embed.badge(PROFILE, o)
                width = int(ET.fromstring(svg).get("width"))
                label, value = embed.metric(PROFILE, metric, "en")
                text = embed.text_width(label, 11) + embed.text_width(value, 11)
                # Four paddings of six, plus the logo and its gap.
                self.assertGreaterEqual(width, text + 24 + 18)

    def test_a_typo_answers_with_a_picture(self):
        # Nobody sees a 400 in an <img>, so an unknown word falls back and a
        # number out of range is clamped rather than refused.
        o = options("bars", style="neon", show="everything", theme="pink",
                    lang="tlh", n="9000", w="4")
        self.assertEqual(o["style"], "plain")
        self.assertEqual(o["show"], "top")
        self.assertEqual(o["theme"], "dark")
        self.assertEqual(o["lang"], "en")
        self.assertEqual(o["n"], 15)
        self.assertEqual(o["w"], 240)
        ET.fromstring(embed.bars(PROFILE, o))

    def test_a_label_is_trimmed_to_the_room_it_has(self):
        long = "Grand Theft Auto: San Andreas - The Definitive Edition"
        for budget in (40, 90, 200):
            with self.subTest(budget):
                self.assertLessEqual(embed.text_width(embed.clip(long, 11, budget), 11),
                                     budget)

    def test_a_custom_label_cannot_carry_markup_out_of_its_box(self):
        o = options("badge", label='<script>"&', logo="0")
        svg = embed.badge(PROFILE, o)
        ET.fromstring(svg)
        self.assertNotIn("<script>", svg)

    def test_the_text_chart_lines_up(self):
        lines = embed.text_bars(PROFILE, options("text", n="3", cells="12")).splitlines()
        rows = lines[1:-1]
        self.assertEqual(len({len(r) for r in rows}), 1, rows)
        # Every row has a bar of exactly the width that was asked for.
        for row in rows:
            self.assertEqual(sum(row.count(c) for c in (embed.FULL, embed.EMPTY)
                                 + embed.PARTIAL[1:]), 12)

    def test_every_named_colour_draws(self):
        # The table is written by hand, and one entry in it was three digits
        # where every other was six. Naming them in a test is not enough: the
        # bad one only failed on the way through ink(), so each has to make a
        # badge.
        for name in embed.BADGE_COLOURS:
            with self.subTest(name):
                svg = embed.badge(PROFILE, options("badge", color=name))
                ET.fromstring(svg)
                self.assertIn(embed.BADGE_COLOURS[name], svg)

    def test_a_banner_shows_the_figures_it_was_asked_for(self):
        o = options("banner", preset="blog", facts="level,deck,hours", lang="en")
        self.assertEqual(o["facts"], ("level", "deck", "hours"))
        svg = embed.banner(PROFILE, o)
        # In that order, left to right. Read off the x of each label and not off
        # the order of the tags: a horizontal strip lays its boxes out from the
        # right edge inwards, so the document holds them backwards on purpose.
        placed = sorted((float(node.get("x")), (node.text or ""))
                        for node in ET.fromstring(svg).iter()
                        if node.tag.endswith("text") and node.get("x"))
        labels = [text for _, text in placed if text.isupper()]
        self.assertEqual(labels, ["LEVEL", "STEAM DECK", "HOURS"])

    def test_a_banner_can_be_asked_for_no_figures(self):
        o = options("banner", preset="blog", facts="none", lang="en")
        self.assertEqual(o["facts"], ())
        svg = embed.banner(PROFILE, o)
        ET.fromstring(svg)
        self.assertIn("Gordziilla", svg)
        self.assertNotIn("HOURS", svg)

    def test_a_typo_in_the_figures_falls_back_rather_than_emptying(self):
        # A banner with no figures is a thing somebody can mean; a banner with
        # no figures because they wrote "horas" is not.
        self.assertEqual(options("banner", facts="horas,jogos")["facts"],
                         embed.DEFAULT_FACTS)
        self.assertEqual(options("banner", facts="")["facts"], embed.DEFAULT_FACTS)
        # Repeats are one box, and five is four.
        self.assertEqual(options("banner", facts="hours,hours")["facts"], ("hours",))
        self.assertEqual(len(options(
            "banner", facts="hours,games,played,level,deck")["facts"]),
            embed.MAX_FACTS)

    def test_a_narrow_banner_drops_a_box_before_it_drops_the_name(self):
        # Four figures, each of them a game title, across the shortest strip
        # there is. Something has to give, and it must not be who this is.
        o = options("banner", preset="wide", facts="top,now,top,hours", lang="en")
        svg = embed.banner(PROFILE, o)
        ET.fromstring(svg)
        self.assertIn("Gordziilla", svg)

    def test_the_colours_are_hex(self):
        for i in range(6):
            self.assertRegex(embed.ramp(i, 6, embed.THEMES["dark"]), r"^#[0-9a-f]{6}$")
        self.assertEqual(embed.colour_of("F0A"), "#ff00aa")
        self.assertEqual(embed.colour_of("steam"), "#66c0f4")
        self.assertIsNone(embed.colour_of("mauve"))
        self.assertEqual(embed.ink("#ffb454"), "#0b0a0e")
        self.assertEqual(embed.ink("#131219"), "#ffffff")
        self.assertEqual(embed.ink("#fff"), embed.ink("#ffffff"))


if __name__ == "__main__":
    unittest.main()


class VersusTest(EmbedTest):
    """Two profiles, which is the one picture here that can be asked a question
    about a pair rather than about a person."""

    def test_the_rows_asked_for_are_the_rows_drawn(self):
        o = options("versus", rows="deck,level,hours", games="0", lang="en")
        self.assertEqual(o["rows"], ("deck", "level", "hours"))
        labels = [(node.text or "") for node in
                  ET.fromstring(embed.versus(PROFILE, RIVAL, o)).iter()
                  if node.tag.endswith("text") and (node.text or "").isupper()]
        self.assertEqual([one for one in labels if one != "VS"],
                         ["STEAM DECK", "LEVEL", "HOURS"])

    def test_a_card_can_be_asked_for_no_rows_and_no_games(self):
        o = options("versus", rows="none", games="0", lang="en")
        self.assertEqual(o["rows"], ())
        svg = embed.versus(PROFILE, RIVAL, o)
        ET.fromstring(svg)
        # Both people are still on it, which is the whole of what is left.
        self.assertIn("Gordziilla", svg)
        self.assertIn("Ana Beatriz", svg)

    def test_only_the_games_both_of_them_own_are_listed(self):
        svg = embed.versus(PROFILE, RIVAL, options("versus", games="10", lang="en"))
        self.assertIn("Counter-Strike 2", svg)
        self.assertIn("Arma 3", svg)
        # Owned by one of them each, and therefore not a comparison.
        self.assertNotIn("Stardew Valley", svg)
        self.assertNotIn("Grand Theft Auto", svg)

    def test_two_avatars_do_not_share_one_clip_path(self):
        svg = embed.versus(PROFILE, RIVAL, options("versus"))
        ids = re.findall(r'<clipPath id="([^"]+)"', svg)
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_a_pair_of_zeros_is_an_empty_bar_and_not_half_each(self):
        theme = embed.THEMES["dark"]
        self.assertNotIn(theme["accent"], embed.split(0, 0, 100, 0, 0, theme))
        self.assertIn(theme["accent"], embed.split(0, 0, 100, 1, 0, theme))


class ArtworkTest(EmbedTest):
    """The large one. Everything a Steam profile can be handed as artwork,
    including a picture the api is never allowed to see."""

    def test_the_figures_asked_for_are_the_figures_drawn(self):
        o = options("artwork", facts="badges,linux", games="0", lang="en")
        self.assertEqual(o["facts"], ("badges", "linux"))
        labels = [(node.text or "") for node in
                  ET.fromstring(embed.artwork(PROFILE, o)).iter()
                  if node.tag.endswith("text") and (node.text or "").isupper()]
        self.assertEqual(sorted(set(labels)), ["BADGES", "LINUX"])

    def test_a_picture_of_the_readers_own_is_a_slot_and_never_a_fetch(self):
        # The one thing that must not happen here is this file arriving with
        # somebody's photograph in it. The api draws an empty slot; the browser
        # fills it, and never sends what it filled it with.
        svg = embed.artwork(PROFILE, options("artwork", bg="own"))
        self.assertIn(f'href="{embed.OWN_SLOT}"', svg)
        self.assertNotIn("base64", svg)
        ET.fromstring(svg)

    def test_a_signature_is_fifteen_characters_and_is_drawn_as_strokes(self):
        o = options("artwork", sign="Gordziilla e mais um tanto")
        self.assertEqual(len(o["sign"]), embed.SIGN_MAX)
        svg = embed.artwork(PROFILE, o)
        ET.fromstring(svg)
        # No font is named for it, because there is no font: a signature that
        # asked for one would be a different signature on every machine.
        self.assertIn("<path", svg)

    def test_a_signature_of_nothing_drawable_is_no_signature(self):
        o = options("artwork", sign="олег")
        self.assertEqual(sign.fits(o["sign"]), "")
        ET.fromstring(embed.artwork(PROFILE, o))

    def test_a_signature_is_shrunk_to_the_width_it_has(self):
        for size in (20, 60, 120):
            with self.subTest(size):
                self.assertLessEqual(sign.width("MMMMMMMMMMMMMMM", size) / size,
                                     sign.width("MMMMMMMMMMMMMMMM", size) / size)

    def test_a_typo_in_the_backdrop_falls_back_rather_than_failing(self):
        o = options("artwork", preset="poster", bg="sunset", fade="9000",
                    blur="-4", games="99")
        self.assertEqual(o["preset"], "wide")
        self.assertEqual(o["bg"], "back")
        self.assertEqual(o["fade"], 100)
        self.assertEqual(o["blur"], 0)
        self.assertEqual(o["games"], 12)
        ET.fromstring(embed.artwork(PROFILE, o))

    def test_the_avatar_backdrop_is_blurred_unless_somebody_said_otherwise(self):
        # 184 pixels across 1920 of them. Left sharp it is a mosaic, so the
        # default is not zero here and is zero everywhere else.
        self.assertEqual(options("artwork", bg="face")["blur"], 22)
        self.assertEqual(options("artwork", bg="face", blur="0")["blur"], 0)
        self.assertEqual(options("artwork", bg="back")["blur"], 0)
