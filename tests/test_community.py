import threading
import unittest

import community


class FakeClock:
    """A clock that only moves when somebody sleeps.

    The pace is eight seconds and there are cases below that need three slots.
    Waiting for them in real time is a suite nobody runs; not exercising the
    pacing at all is a suite that tests the wrong thing. So the pacer's two
    time primitives are swapped for these, and sleeping becomes the only way
    time passes."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


class CommunityPaceTest(unittest.TestCase):
    """The host is never called here. What is worth testing is the budget the
    three readers share, which is decided before any request goes out."""

    def setUp(self):
        self.clock = FakeClock()
        self.old = (community._clock, community._sleep)
        community._clock = self.clock.monotonic
        community._sleep = self.clock.sleep
        community.reset()
        self.addCleanup(self.restore)

    def restore(self):
        community._clock, community._sleep = self.old
        community.reset()

    def test_two_readers_share_one_interval(self):
        """cards.py and inv.py asking back to back is two requests one interval
        apart, not two at once. This is the whole reason the module exists."""
        self.assertTrue(community.reserve())
        start = self.clock.now
        self.assertTrue(community.reserve())
        self.assertGreaterEqual(self.clock.now - start, community.INTERVAL)

    def test_a_claimed_slot_costs_the_next_reader_the_full_wait(self):
        """A visitor-facing scrape never waits, and the crawl behind it pays
        the interval anyway. The host sees the same average rate either way."""
        community.claim()
        self.assertEqual(self.clock.slept, 0.0)
        start = self.clock.now
        self.assertTrue(community.reserve())
        self.assertGreaterEqual(self.clock.now - start, community.INTERVAL)

    def test_a_refusal_anywhere_cools_the_host_for_everybody(self):
        community.note_429()
        self.assertAlmostEqual(community.cooling(), community.BACKOFF_MIN, places=1)
        self.clock.sleep(community.BACKOFF_MIN)
        self.assertEqual(community.cooling(), 0.0)

    def test_a_caller_that_will_not_wait_is_told_so_instead_of_queued(self):
        """set_of passes max_wait so a page open answers off disk rather than
        holding a request open behind the crawl."""
        self.assertTrue(community.reserve())
        self.assertFalse(community.reserve(max_wait=0))
        # Refusing must not have cost the caller the wait it refused.
        self.assertEqual(self.clock.slept, 0.0)

    def test_the_crawl_yields_to_a_page(self):
        """A background reader waits while somebody has a page open, however
        long that takes, because the page is the one with a person on it."""
        community.page_enter()
        done = threading.Event()
        result = []

        def crawl():
            result.append(community.reserve(background=True))
            done.set()

        worker = threading.Thread(target=crawl, daemon=True)
        worker.start()
        # It cannot finish while the page job stands. The fake clock only moves
        # in the yield loop, so this is a statement about ordering and not a
        # race: given a real chance to run, it still must not return.
        self.assertFalse(done.wait(0.2))
        community.page_leave()
        self.assertTrue(done.wait(2))
        self.assertEqual(result, [True])

    def test_reset_forgets_a_cooldown(self):
        """Without this the suite carries one case's 429 into the next, which
        is the failure the old cards._cool_until assignment used to prevent."""
        community.note_429()
        community.reset()
        self.assertEqual(community.cooling(), 0.0)


if __name__ == "__main__":
    unittest.main()
