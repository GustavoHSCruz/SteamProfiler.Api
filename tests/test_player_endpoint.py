"""The cross-origin response surface consumed by SteamProfiler.Player."""
import io
import json
import unittest

import api


class PublicEndpointTest(unittest.TestCase):
    @staticmethod
    def handler(path="/player"):
        """A handler without a socket; enough surface to inspect its headers."""
        handler = object.__new__(api.Handler)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.status = None
        handler.sent_headers = {}
        handler.send_response = lambda status: setattr(handler, "status", status)
        handler.send_header = lambda name, value: handler.sent_headers.__setitem__(name, value)
        handler.end_headers = lambda: None
        return handler

    def test_player_json_is_cross_origin_and_versioned(self):
        handler = self.handler()
        handler.public_cors = True
        payload = {"version": 1, "appid": 620, "state": "ready"}
        handler.send_json(200, payload, ttl=600)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.sent_headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(handler.sent_headers["Cache-Control"], "public, max-age=600")
        self.assertEqual(json.loads(handler.wfile.getvalue()), payload)

    def test_other_json_does_not_publish_cors(self):
        handler = self.handler()
        handler.send_json(400, {"error": "bad"})
        self.assertNotIn("Access-Control-Allow-Origin", handler.sent_headers)

    def test_options_only_publishes_versioned_contracts(self):
        handler = self.handler()
        handler.do_OPTIONS()
        self.assertEqual(handler.status, 204)
        self.assertEqual(handler.sent_headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("GET", handler.sent_headers["Access-Control-Allow-Methods"])

        companion = self.handler("/companion")
        companion.do_OPTIONS()
        self.assertEqual(companion.status, 204)
        self.assertEqual(companion.sent_headers["Access-Control-Allow-Origin"], "*")

        other = self.handler("/profile")
        other.send_empty = lambda status: setattr(other, "status", status)
        other.do_OPTIONS()
        self.assertEqual(other.status, 404)


if __name__ == "__main__":
    unittest.main()
