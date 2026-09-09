import unittest

import fetch


# A real answer, trimmed to the tags parse_xml reads. Kept verbatim rather than
# tidied: the CDATA, the <br/> inside stateMessage and the empty <location/>
# are all shapes Steam really sends, and all three used to be handled wrong.
XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<profile>
  <steamID64>76561198000000000</steamID64>
  <steamID><![CDATA[Gordziilla]]></steamID>
  <onlineState>in-game</onlineState>
  <stateMessage><![CDATA[In-Game<br/>Counter-Strike 2]]></stateMessage>
  <privacyState>public</privacyState>
  <visibilityState>3</visibilityState>
  <avatarIcon><![CDATA[https://example.invalid/a.jpg]]></avatarIcon>
  <vacBanned>0</vacBanned>
  <tradeBanState>None</tradeBanState>
  <isLimitedAccount>0</isLimitedAccount>
  <customURL><![CDATA[example]]></customURL>
  <memberSince>March 15, 2013</memberSince>
  <steamRating><![CDATA[9.5]]></steamRating>
  <summary><![CDATA[one line<br/>another line]]></summary>
  <location></location>
  <realname></realname>
</profile>"""


class ParseXmlTest(unittest.TestCase):
    """Steam is never called here. The document is the interesting part and it
    arrives as text, so the parsing of it is a pure function on purpose."""

    def test_it_reads_the_fields_that_used_to_be_thrown_away(self):
        got = fetch.parse_xml(XML)
        self.assertEqual(got["persona"], "Gordziilla")
        self.assertEqual(got["custom_url"], "example")
        self.assertEqual(got["privacy"], "public")
        self.assertEqual(got["online"], "in-game")
        self.assertEqual(got["rating"], "9.5")
        self.assertEqual(got["member_since"], "March 15, 2013")

    def test_a_break_becomes_a_newline_rather_than_running_two_words_together(self):
        """strip_tags turns <br/> into a newline, so the page can print the
        first line of a status without "In-GameCounter-Strike 2"."""
        self.assertEqual(fetch.parse_xml(XML)["status"], "In-Game\nCounter-Strike 2")

    def test_an_empty_field_is_absent_rather_than_empty(self):
        """Steam sends <location></location> on a profile that never set one.
        Keeping that as "" would give the page a blank line to draw instead of
        a block to leave out."""
        got = fetch.parse_xml(XML)
        self.assertNotIn("location", got)
        self.assertNotIn("realname", got)

    def test_limited_is_false_and_not_missing(self):
        """The good, common case. It travels as a real False because
        build_profile filters on `is not None`; a truth test anywhere in that
        chain turns "this account is fine" into "nobody knows"."""
        got = fetch.parse_xml(XML)
        self.assertIs(got["limited"], False)
        self.assertIs(got["vac_xml"], False)

    def test_a_document_that_says_nothing_answers_nothing(self):
        """A private profile, and a request that came back empty because the
        host was cooling, are both this. Neither may raise: the profile build
        goes on without the block."""
        self.assertEqual(fetch.parse_xml(""), {})
        self.assertEqual(fetch.parse_xml("<profile></profile>"), {})

    def test_a_flag_steam_did_not_send_stays_absent(self):
        """Absent is not false. A document with no isLimitedAccount is a
        document that did not say, and saying "not limited" for it would be
        inventing the answer."""
        self.assertNotIn("limited", fetch.parse_xml("<profile><steamID>x</steamID></profile>"))


if __name__ == "__main__":
    unittest.main()
