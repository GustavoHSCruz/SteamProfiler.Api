"""/api/apps - the store cache read the franchise screens ask for.

The three things worth pinning down are the three that would break a screen
quietly: that a cold appid is answered rather than dropped, that a review total
becomes a percentage the page can print, and that the trailer the store leads
with is the one that comes back.
"""
import json
import tempfile
import unittest
from pathlib import Path

import api
import meta


class AppsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = meta.DATA_DIR
        self.old_db_path = meta.DB_PATH
        self.old_ready = meta._ready
        meta.DATA_DIR = Path(self.temp.name)
        meta.DB_PATH = meta.DATA_DIR / "meta.db"
        meta._ready = False
        meta.init()

    def tearDown(self):
        meta.DATA_DIR = self.old_data_dir
        meta.DB_PATH = self.old_db_path
        meta._ready = self.old_ready
        self.temp.cleanup()

    def known(self, appid, **catalog):
        """Write one app into the cache the way the detail pass would."""
        body = {
            "name": catalog.pop("name", None),
            "release": {"coming_soon": False, "date": catalog.pop("date", None)},
            "movies": catalog.pop("movies", []),
            "categories": catalog.pop("categories", []),
            "genres": catalog.pop("genres", []),
            "achievements": catalog.pop("achievements", None),
        }
        meta._save(
            appid,
            name=body["name"],
            exists_on_store=1,
            year=catalog.pop("year", None),
            free=0,
            detail_at=meta._stamp(),
            catalog_at=meta._stamp(),
            catalog=json.dumps(body),
            reviews=json.dumps(catalog.pop("reviews")) if "reviews" in catalog else None,
            reviews_at=meta._stamp() if "reviews" in catalog else None,
        )

    def test_cold_appid_is_answered_rather_than_missing(self):
        # A screen draws every row from its own table and fills the numbers in
        # from here, so an app nobody has read yet has to come back as a row
        # that says so - not as an absent key the page would have to guess at.
        out = api.do_apps([70, 220], "us", live=())
        self.assertEqual(sorted(out["apps"]), ["220", "70"])
        for row in out["apps"].values():
            self.assertFalse(row["known"])
            self.assertIsNone(row["name"])
            self.assertIsNone(row["reviews"])

    def test_reviews_become_a_percentage(self):
        self.known(220, name="Half-Life 2", year=2004, date="16 Nov, 2004",
                   reviews={"total": 200, "positive": 190, "negative": 10,
                            "score": 9, "description": "Overwhelmingly Positive"})
        row = api.do_apps([220], "us", live=())["apps"]["220"]
        self.assertTrue(row["known"])
        self.assertEqual(row["name"], "Half-Life 2")
        self.assertEqual(row["released"], "16 Nov, 2004")
        self.assertEqual(row["year"], 2004)
        self.assertEqual(row["reviews"]["positive_pct"], 95.0)
        self.assertEqual(row["reviews"]["total"], 200)

    def test_no_reviews_is_none_and_not_a_zero(self):
        # Nought reviews and "the review pass has not run" are different facts,
        # and only one of them may be printed as a percentage.
        self.known(400, name="Portal", reviews={"total": 0, "positive": 0})
        self.assertIsNone(api.do_apps([400], "us", live=())["apps"]["400"]["reviews"])

    def test_review_refresh_keeps_a_separate_latest_review_sample(self):
        old_get = meta._get_url
        old_pace = meta._pace
        calls = []
        try:
            def fake_get(url, timeout=None):
                calls.append(url)
                recent = "filter=recent" in url
                if recent:
                    return {"reviews": [
                        {"voted_up": index < 90, "timestamp_created": 1700000000 + index}
                        for index in range(100)
                    ]}
                return {"query_summary": {
                    "review_score": 9,
                    "review_score_desc": "Very Positive",
                    "total_positive": 980,
                    "total_negative": 20,
                    "total_reviews": 1000,
                }}
            meta._get_url = fake_get
            meta._pace = lambda **kwargs: True
            self.assertTrue(meta._do_reviews(620))
        finally:
            meta._get_url = old_get
            meta._pace = old_pace
        reviews = meta.lookup([620])[620]["reviews"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(reviews["total"], 1000)
        self.assertEqual(reviews["recent"]["sample"], "latest")
        self.assertEqual(reviews["recent"]["positive"], 90)

    def test_companion_refreshes_the_old_lifetime_only_review_shape(self):
        self.known(621, name="Portal 2", reviews={
            "total": 1000, "positive": 980, "negative": 20,
        })
        old_detail = meta._do_detail
        old_reviews = meta._do_reviews
        old_pace = meta._pace
        refreshed = []
        try:
            meta._do_detail = lambda *args, **kwargs: True
            meta._do_reviews = lambda appid, **kwargs: refreshed.append(appid) or True
            meta._pace = lambda **kwargs: True
            meta.public_catalog(621, "us", "en")
        finally:
            meta._do_detail = old_detail
            meta._do_reviews = old_reviews
            meta._pace = old_pace
        self.assertEqual(refreshed, [621])

    def test_trailer_is_the_one_the_store_leads_with(self):
        self.known(620, name="Portal 2", movies=[
            {"id": 1, "name": "second", "thumbnail": "b", "highlight": False},
            {"id": 2, "name": "first", "thumbnail": "a", "highlight": True},
        ])
        row = api.do_apps([620], "us", live=())["apps"]["620"]
        self.assertEqual(row["trailer"], {"id": 2, "name": "first", "thumb": "a"})

    def test_trailer_carries_maximum_and_adaptive_media(self):
        self.known(620, name="Portal 2", movies=[{
            "id": 2, "name": "first", "thumbnail": "a", "highlight": True,
            "mp4": {"max": "https://cdn/movie_max.mp4", "480": "https://cdn/movie480.mp4"},
            "webm": {"max": "https://cdn/movie_max.webm"},
            "hls_h264": "https://video/master.m3u8",
            "dash_h264": "https://video/manifest.mpd",
        }])
        trailer = api.do_apps([620], "us", live=())["apps"]["620"]["trailer"]
        self.assertEqual(trailer["max_mp4"], "https://cdn/movie_max.mp4")
        self.assertEqual(trailer["max_webm"], "https://cdn/movie_max.webm")
        self.assertEqual(trailer["sd_mp4"], "https://cdn/movie480.mp4")
        self.assertEqual(trailer["hls"], "https://video/master.m3u8")
        self.assertEqual(trailer["dash"], "https://video/manifest.mpd")

    def test_missing_media_refresh_jumps_ahead_of_price_work(self):
        old_worker = meta._worker
        media_id, detail_id, price_id = 99993, 99992, 99991
        try:
            meta._worker = object()
            with meta._queue_lock:
                meta._price_wanted["br"].append(price_id)
                meta._queued_price.add((price_id, "br"))
                meta._detail_wanted.append(detail_id)
                meta._queued_detail.add(detail_id)
            meta.want_media([media_id])
            self.assertEqual(meta._next_job(), ("detail", None, media_id))
        finally:
            with meta._queue_lock:
                for queue, value in ((meta._price_wanted["br"], price_id),
                                     (meta._detail_wanted, detail_id),
                                     (meta._detail_wanted, media_id)):
                    while value in queue:
                        queue.remove(value)
                meta._queued_price.discard((price_id, "br"))
                meta._queued_detail.discard(detail_id)
                meta._queued_detail.discard(media_id)
                meta._urgent_detail.discard(media_id)
            meta._worker = old_worker

    def test_no_movies_is_no_trailer_button(self):
        self.known(70, name="Half-Life")
        self.assertIsNone(api.do_apps([70], "us", live=())["apps"]["70"]["trailer"])

    def test_player_contract_is_small_and_versioned(self):
        self.known(620, name="Portal 2", movies=[{
            "id": 2, "name": "Portal 2 trailer", "thumbnail": "poster.jpg",
            "highlight": True,
            "mp4": {"max": "https://cdn/movie_max.mp4", "480": "https://cdn/movie480.mp4"},
            "webm": {"max": "https://cdn/movie_max.webm"},
            "hls_h264": "https://video/master.m3u8",
            "dash_h264": "https://video/manifest.mpd",
        }])
        out = api.do_player(620)
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["title"], "Portal 2 trailer")
        self.assertEqual(out["poster"], "poster.jpg")
        self.assertEqual(out["media"]["mp4"], [
            "https://cdn/movie_max.mp4", "https://cdn/movie480.mp4",
        ])
        self.assertEqual(out["media"]["webm"], ["https://cdn/movie_max.webm"])
        self.assertEqual(out["media"]["hls"], "https://video/master.m3u8")
        self.assertEqual(out["attribution"]["label"], "steamprofiler.org")
        self.assertNotIn("reviews", out)
        self.assertNotIn("price", out)

    def test_player_distinguishes_pending_from_no_trailer(self):
        self.assertEqual(api.do_player(99999)["state"], "pending")
        self.known(70, name="Half-Life")
        out = api.do_player(70)
        self.assertEqual(out["state"], "absent")
        self.assertIsNone(out["media"])

    def test_companion_contract_is_small_and_versioned(self):
        self.known(620, name="Portal 2", year=2011, date="19 Apr, 2011",
                   categories=[{"id": 2}, {"id": 22}], genres=[{"id": 3}],
                   achievements={"total": 51},
                   movies=[{"id": 2, "name": "Trailer", "highlight": True,
                            "thumbnail": "https://cdn/poster.jpg",
                            "mp4": {"max": "https://cdn/movie.mp4"}}],
                   reviews={"total": 1000, "positive": 980, "negative": 20,
                            "score": 9, "description": "Overwhelmingly Positive",
                            "recent": {"sample": "latest", "total": 100,
                                       "positive": 91, "negative": 9,
                                       "oldest_at": 1700000000}})
        old_catalog = meta.public_catalog
        old_players = api.fetch.fetch_current_players
        old_news = api.fetch.fetch_public_news
        try:
            meta.public_catalog = lambda appid, cc, language: meta.lookup([appid])[appid]
            api.fetch.fetch_current_players = lambda appid: {"players": 12345}
            api.fetch.fetch_public_news = lambda appid: [{
                "date": 1700000000, "title": "A real update",
                "url": "https://store.steampowered.com/news/app/620/view/1",
                "feed_name": "steam_community_announcements",
            }]
            out = api.do_companion(620, "pt")
        finally:
            meta.public_catalog = old_catalog
            api.fetch.fetch_current_players = old_players
            api.fetch.fetch_public_news = old_news

        self.assertEqual(out["version"], 1)
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["game"]["name"], "Portal 2")
        self.assertEqual(out["reviews"]["positive_pct"], 98.0)
        self.assertEqual(out["reviews"]["recent"]["positive_pct"], 91.0)
        self.assertEqual(out["players"], 12345)
        self.assertEqual(out["game"]["categories"], [2, 22])
        self.assertEqual(out["game"]["achievements"], 51)
        self.assertEqual(out["activity"]["latest_news_at"], 1700000000)
        self.assertEqual(out["trailer"]["media"]["mp4"], ["https://cdn/movie.mp4"])
        self.assertEqual(out["links"]["analysis"], "https://steamprofiler.org/g/620")
        self.assertEqual(out["attribution"]["label"], "steamprofiler.org")
        self.assertNotIn("news", out)

    def test_personal_companion_keeps_only_progress_for_this_game(self):
        old_profile = api.do_profile
        old_achievements = api.fetch.fetch_achievements
        try:
            api.do_profile = lambda steamid: {
                "top_games": [], "unplayed": [], "private_stat_block": {"not": "sent"},
                "library": [{
                    "appid": 620, "name": "Portal 2", "hours": 12.5,
                    "minutes_2weeks": 120, "last_played": "2026-09-10",
                }],
            }
            api.fetch.fetch_achievements = lambda appid: {
                "unlocked": 8, "total": 10, "completion": 80.0, "missing": 2,
                "easiest_missing": {"name": "Next", "rarity": 45.0,
                                     "description": "not sent", "icon": "not sent"},
                "hardest_missing": {"name": "Wall", "rarity": 1.0},
                "list": [{"name": "not sent"}],
            }
            out = api.do_companion_profile("76561198000000000", 620)
        finally:
            api.do_profile = old_profile
            api.fetch.fetch_achievements = old_achievements
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["hours"], 12.5)
        self.assertEqual(out["achievements"]["completion"], 80.0)
        self.assertEqual(out["achievements"]["easiest_missing"],
                         {"name": "Next", "rarity": 45.0})
        self.assertNotIn("list", out["achievements"])
        self.assertNotIn("private_stat_block", out)


if __name__ == "__main__":
    unittest.main()
