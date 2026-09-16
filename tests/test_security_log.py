import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import api
import bans
import census
import security_log as log
import store

BROWSER = "Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36"


class SecurityLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = log.DATA_DIR, log.DB_PATH, store._salt
        log.DATA_DIR = Path(self.temp.name)
        log.DB_PATH = log.DATA_DIR / "security.db"
        store._salt = "security-test-salt"
        log._pending.clear()
        self.stats = log._dropped, log._malformed, log._last_received
        log._dropped = log._malformed = log._last_received = 0
        log.init()

    def tearDown(self):
        log.DATA_DIR, log.DB_PATH, store._salt = self.saved
        log._pending.clear()
        log._dropped, log._malformed, log._last_received = self.stats
        self.temp.cleanup()

    def raw(self, uri="/api/resolve?q=person", **overrides):
        return {"address": "198.51.100.7", "request": f"GET {uri} HTTP/1.1",
                "ua": "curl/8.0", "status": "200", "duration": "0.125",
                "bytes": "70", "country": "BR", **overrides}

    def record(self, raw):
        log.ingest(b"<190>Sep 15 12:00:00 spsecurity: " + json.dumps(raw).encode())

    def test_every_route_client_and_status_is_retained(self):
        for path in ("/g/440", "/about", "/u/someone", "/api/profile?id=76561198000000001",
                     "/style.css", "/favicon.svg", "/art/440.jpg", "/healthz", "/_gate"):
            for ua in (BROWSER, "", "custom-client", "curl/8.0"):
                for status in ("200", "204", "301", "304", "400", "403", "404", "429", "500"):
                    with self.subTest(path=path, ua=ua, status=status):
                        item = log.event(self.raw(path, ua=ua, status=status))
                        self.assertIsNotNone(item)
                        self.assertEqual(item["status"], int(status))
                        self.assertEqual(item["ua"], ua)
        self.record(self.raw("/g/440", ua=BROWSER))
        self.assertEqual(log.report()["items"][0]["uri"], "/g/440")

    def test_query_values_are_retained_without_guessing_whether_they_are_probes(self):
        self.record(self.raw("/about?sp-security-check=20260915&custom=benign&token=hidden"))
        row = log.report()["items"][0]
        self.assertIn("sp-security-check=20260915", row["uri"])
        self.assertIn("custom=benign", row["uri"])
        self.assertNotIn("hidden", row["uri"])
        self.record(self.raw("/about?" + "&".join(f"p{i}=v{i}" for i in range(150))))
        self.assertEqual(log.report()["total"], 2)
        self.assertEqual(log.report()["malformed"], 0)

    def test_unusual_request_targets_and_custom_methods_still_have_records(self):
        self.record(self.raw(request="M-SEARCH //[?custom=value HTTP/1.1", ua=BROWSER, status="405"))
        self.record(self.raw(request="get /g/440 HTTP/1.1", ua=BROWSER, status="405"))
        rows = log.report()["items"]
        self.assertEqual({r["method"] for r in rows}, {"M-SEARCH", "get"})
        self.assertTrue(any(r["uri"].startswith("//[") for r in rows))
        self.record(self.raw(request="garbage", ua=BROWSER, status="400"))
        row = log.report()["items"][0]
        self.assertEqual(row["method"], "?")
        self.assertEqual(row["uri"], "garbage")
        self.assertEqual(log.report()["malformed"], 0)

    def test_successful_and_redirected_game_requests_from_declared_bots_are_retained(self):
        for ua, kind in (
            ("Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)", "tool"),
            ("AhrefsBot/7.0", "tool"),
            ("ExampleCrawler/1.0", "tool"),
            ("ExampleSpider/1.0", "tool"),
            ("Scrapy/2.0", "tool"),
            ("Googlebot/2.1", "search"),
            ("bingbot/2.0", "search"),
            ("ClaudeBot/1.0", "ai"),
            ("GPTBot/1.0", "ai"),
        ):
            for status in ("200", "301", "304"):
                with self.subTest(ua=ua, status=status):
                    self.record(self.raw("/g/70", ua=ua, status=status))
                    row = log.report()["items"][0]
                    self.assertEqual(row["uri"], "/g/70")
                    self.assertEqual(row["kind"], kind)
                    self.assertEqual(row["status"], int(status))
                    self.assertEqual(row["signals"], [])
                    # Keep the shared census classification consistent, and
                    # preserve AI/search precedence over generic bot names.
                    self.assertEqual(census._classify({"scan": 0, "ua": ua,
                                                      "fonts": 0, "assets": 0})[0], kind)

    def test_tool_requests_keep_evidence_and_hide_identity_and_credentials(self):
        self.record(self.raw("/u/person/vs/other?appid=440&token=secret&q=private&x=benign"))
        row = log.report()["items"][0]
        self.assertEqual(row["kind"], "tool")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["duration_ms"], 125)
        self.assertIn("/u/[profile]/vs/[profile]", row["uri"])
        self.assertIn("appid=440", row["uri"])
        self.assertIn("x=benign", row["uri"])
        for sensitive in ("person", "other", "secret", "private", "198.51.100.7"):
            self.assertNotIn(sensitive, json.dumps(row))
        with log._connect() as con:
            self.assertNotIn("address", {r[1] for r in con.execute("PRAGMA table_info(requests)")})

    def test_encoded_probes_and_final_http_response_are_visible(self):
        self.record(self.raw("/download?file=%252e%252e%252fetc%252fpasswd&password=never-show",
                             ua=BROWSER, status="403"))
        row = log.report()["items"][0]
        self.assertEqual(row["kind"], "scanner")
        self.assertIn("traversal", row["signals"])
        self.assertIn("passwd", row["uri"])
        self.assertNotIn("never-show", row["uri"])
        self.assertEqual(row["status"], 403)
        self.record(self.raw("/search?input=UNION%20SELECT%201", ua=BROWSER))
        self.assertIn("sql", log.report()["items"][0]["signals"])
        self.record(self.raw("/search?input=or+1=1", ua=BROWSER))
        self.assertIn("sql", log.report()["items"][0]["signals"])

    def test_original_post_method_is_preserved_for_an_error_page(self):
        self.record(self.raw("/.env", request="POST /.env HTTP/1.1", ua=BROWSER, status="403"))
        row = log.report()["items"][0]
        self.assertEqual(row["method"], "POST")
        self.assertIn("secrets", row["signals"])

    def test_error_from_an_unknown_client_and_rate_limit_are_recorded(self):
        self.record(self.raw("/api/profile?id=76561198000000001", ua=BROWSER, status="429"))
        row = log.report()["items"][0]
        self.assertEqual(row["kind"], "unknown")
        self.assertIn("rate", row["signals"])
        self.assertNotIn("76561198000000001", row["uri"])

    def test_user_agent_cannot_leak_bearer_credentials_or_email(self):
        self.record(self.raw(ua="curl/8.0 Bearer topsecret contact=person@example.org"))
        row = log.report()["items"][0]
        self.assertNotIn("topsecret", row["ua"])
        self.assertNotIn("person@example.org", row["ua"])

    def test_filters_grouping_and_cursor_pagination(self):
        for _ in range(101):
            self.record(self.raw())
        first = log.report()
        self.assertEqual(first["total"], 101)
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(first["actors"][0]["requests"], 101)
        second = log.report(before=first["next_before"])
        self.assertEqual(len(second["items"]), 1)
        self.assertTrue({r["id"] for r in first["items"]}.isdisjoint({r["id"] for r in second["items"]}))
        self.assertEqual(log.report(kind="scanner")["total"], 0)
        self.assertEqual(log.report(actor=first["actors"][0]["actor"], status="2xx", q="resolve")["total"], 101)
        self.assertEqual(log.report(q="%'")["total"], 0)
        with self.assertRaises(ValueError):
            log.report(actor="' OR 1=1")

    def test_screens_group_document_api_json_and_assets_in_one_visit(self):
        base = time.time()
        self.record(self.raw("/g/440", at=base, dest="document", type="text/html",
                             status="200", ua=BROWSER))
        for index in range(5):
            self.record(self.raw(f"/api/game/{index}.json", at=base + .2 + index / 10,
                                 referer="https://steamprofiler.org/g/440", host="steamprofiler.org",
                                 ua=BROWSER, type="application/json"))
        for index in range(5):
            self.record(self.raw(f"/style-{index}.css", at=base + 1 + index / 10,
                                 referer="https://steamprofiler.org/g/440", host="steamprofiler.org",
                                 ua=BROWSER, type="text/css"))
        report = log.screens_report()
        self.assertEqual(report["total"], 1)
        visit = report["items"][0]
        self.assertEqual(visit["uri"], "/g/440")
        self.assertEqual(visit["request_count"], 11)
        self.assertEqual(visit["counts"], {"document": 1, "api": 5, "asset": 5, "other": 0})
        self.assertEqual(report["unassigned_total"], 0)
        details = log.screens_report(visit=visit["id"])
        self.assertEqual(details["total"], 11)
        self.assertEqual(len(details["items"]), 11)
        self.assertEqual(log.screens_report(q="style-2.css")["total"], 1)
        hidden = log.screens_report(exclude=visit["actor"])
        self.assertEqual(hidden["total"], 0)
        self.assertEqual(hidden["request_total"], 0)
        with self.assertRaises(ValueError):
            log.screens_report(exclude="not-a-hash")

    def test_screens_keep_parallel_pages_separate_and_leave_missing_context_unassigned(self):
        base = time.time()
        for page, at in (("/g/440", base), ("/g/570", base + 1)):
            self.record(self.raw(page, at=at, dest="document", type="text/html", ua=BROWSER))
            self.record(self.raw("/api/status", at=at + .1, referer=f"https://steamprofiler.org{page}",
                                 host="steamprofiler.org", ua=BROWSER, type="application/json"))
        self.record(self.raw("/boot.js", at=base + 2, ua=BROWSER, type="application/javascript"))
        report = log.screens_report()
        self.assertEqual(report["total"], 2)
        self.assertEqual({item["uri"] for item in report["items"]}, {"/g/440", "/g/570"})
        self.assertEqual(report["unassigned_total"], 1)
        orphan = log.screens_report(unassigned=True)
        self.assertEqual(orphan["total"], 1)
        self.assertEqual(orphan["items"][0]["uri"], "/boot.js")

    def test_screens_do_not_use_external_referrer_or_guess_a_page_from_actor(self):
        self.record(self.raw("/main.js", at=time.time(), ua=BROWSER,
                             referer="https://other.example/g/440", host="steamprofiler.org"))
        report = log.screens_report()
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["unassigned_total"], 1)
        self.assertEqual(log.screens_report(unassigned=True)["items"][0]["page"], "")

    def test_expiration_storage_limit_and_buffer_limit(self):
        with patch.object(log.time, "time", return_value=time.time() - 8 * 86400):
            self.record(self.raw())
        self.assertEqual(log.report()["total"], 0)
        with patch.object(log, "MAX_EVENTS", 2):
            for i in range(3):
                self.record(self.raw(f"/probe{i}"))
            report = log.report()
            self.assertEqual(report["total"], 2)
            self.assertEqual(report["items"][-1]["uri"], "/probe1")
        with patch.object(log, "MAX_PENDING", 1):
            self.record(self.raw())
            self.record(self.raw())
            self.assertEqual(log.report()["dropped"], 1)

    def test_malformed_syslog_does_not_break_the_collector(self):
        for packet in (b"invalid", b"spsecurity: []", b'spsecurity: {"request":"bad"}',
                       b"spsecurity: " + json.dumps(self.raw(bytes=float("inf"))).encode()):
            log.ingest(packet)
        self.record(self.raw())
        self.assertEqual(log.report()["malformed"], 4)
        self.assertEqual(log.report()["total"], 1)

    def test_database_failure_requeues_pending_events(self):
        self.record(self.raw())
        with patch.object(log, "_connect", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                log.flush()
        self.assertEqual(log.report()["total"], 1)

    def test_reset_clears_stored_and_pending_requests_and_keeps_collecting(self):
        self.record(self.raw("/stored"))
        log.flush()
        self.record(self.raw("/pending"))
        log.reset()
        report = log.report()
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["actors"], [])
        self.assertIsNone(report["next_before"])
        self.record(self.raw("/new"))
        self.assertEqual([r["uri"] for r in log.report()["items"]], ["/new"])

    def test_reset_failure_preserves_stored_and_pending_evidence(self):
        self.record(self.raw("/stored"))
        log.flush()
        self.record(self.raw("/pending"))
        with patch.object(log, "_connect", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                log.reset()
        self.assertEqual({r["uri"] for r in log.report()["items"]}, {"/stored", "/pending"})

    def test_reset_waits_for_an_inflight_flush(self):
        self.record(self.raw("/old"))
        entered = threading.Event()
        release = threading.Event()
        resetting = threading.Event()
        errors = []
        connect = log._connect

        def paused_connect():
            if threading.current_thread().name == "test-flush":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test flush not released")
            return connect()

        def run(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)

        def reset():
            resetting.set()
            log.reset()

        with patch.object(log, "_connect", side_effect=paused_connect):
            writer = threading.Thread(target=lambda: run(log.flush), name="test-flush")
            cleaner = threading.Thread(target=lambda: run(reset))
            writer.start()
            try:
                self.assertTrue(entered.wait(5))
                cleaner.start()
                self.assertTrue(resetting.wait(5))
                # The batch has left the queue but has not reached SQLite.
                self.record(self.raw("/pending"))
            finally:
                release.set()
                writer.join(5)
                if cleaner.ident is not None:
                    cleaner.join(5)
        self.assertFalse(writer.is_alive())
        self.assertFalse(cleaner.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(log.report()["total"], 0)

    def test_reset_endpoint_requires_admin_and_returns_updated_history(self):
        handler = object.__new__(api.Handler)
        handler.path = "/admin/security/reset"
        handler.read_json = lambda: {}
        handler.send_json = lambda status, body: (status, body)
        with patch.object(api, "ADMIN_TOKEN", "test-admin-token"), \
                patch.object(log, "reset") as reset, \
                patch.object(log, "report", return_value={"items": [], "total": 0}) as report, \
                patch.object(handler, "send_json", side_effect=handler.send_json) as send:
            handler.headers = {"Authorization": "Bearer wrong"}
            handler.do_POST()
            self.assertEqual(send.call_args[0][0], 401)
            reset.assert_not_called()
            report.assert_not_called()
            handler.headers = {"Authorization": "Bearer test-admin-token"}
            self.assertEqual(handler.do_POST(), (200, {"items": [], "total": 0}))
            reset.assert_called_once_with()
            report.assert_called_once_with()

    def test_private_endpoint_requires_admin(self):
        handler = object.__new__(api.Handler)
        handler.path = "/admin/security"
        handler.headers = {"Authorization": "Bearer wrong"}
        handler.send_json = lambda status, body: None
        with patch.object(api, "ADMIN_TOKEN", "test-admin-token"), \
                patch.object(log, "report") as report, \
                patch.object(handler, "send_json") as send:
            handler.do_GET()
            self.assertEqual(send.call_args[0][0], 401)
            report.assert_not_called()
            handler.headers = {"Authorization": "Bearer test-admin-token"}
            report.return_value = {"items": []}
            handler.do_GET()
            self.assertEqual(send.call_args[0], (200, {"items": []}))

    def test_security_block_endpoint_bans_the_displayed_hash(self):
        handler = object.__new__(api.Handler)
        handler.path = "/admin/security/block"
        handler.headers = {"Authorization": "Bearer test-admin-token"}
        handler.read_json = lambda: {"actor": "a" * 64, "path": "/g/440"}
        handler.send_json = lambda status, body: (status, body)
        with patch.object(api, "ADMIN_TOKEN", "test-admin-token"), \
                patch.object(bans, "ban_hash", return_value=123) as ban_hash, \
                patch.object(bans, "by_hash", return_value={"until_at": "soon"}) as by_hash:
            self.assertEqual(handler.do_POST(), (200, {"ok": True, "until_at": "soon"}))
            ban_hash.assert_called_once_with("a" * 64, reason="admin", path="/g/440")
            by_hash.assert_called_once_with("a" * 64)


if __name__ == "__main__":
    unittest.main()
