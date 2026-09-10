"""embed.py - the pictures that leave the site.

Four things would break quietly here, and each of them breaks somewhere nobody
can fix it: in a README, in a forum post, on somebody else's blog.

    * a file that is not well-formed XML draws as nothing at all
    * a file that references anything outside itself draws as a broken box on
      GitHub, which proxies images, and refuses to become a PNG in the browser
    * a badge narrower than its own text clips it
    * a query string with a typo in it must still answer with a picture
"""
import unittest
import xml.etree.ElementTree as ET

import embed


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
        self.old_thumb, self.old_avatar = embed.art.thumb, embed.art.avatar
        embed.art.thumb = lambda appid: None
        embed.art.avatar = lambda url: None

    def tearDown(self):
        embed.art.thumb, embed.art.avatar = self.old_thumb, self.old_avatar

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
                           ("badge", embed.badge)):
            with self.subTest(kind):
                ET.fromstring(draw(EMPTY, options(kind, lang="pt")))
        self.assertIn("steamprofiler.org", embed.text_bars(EMPTY, options("text")))

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
