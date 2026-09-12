import threading
import time
import unittest

import fetch


class GatherTest(unittest.TestCase):
    """Steam is never called here. gather() is the scheduling, and the
    scheduling is what a build's answers and a build's failures hang off."""

    def test_it_answers_under_the_names_it_was_given(self):
        got = fetch.gather({"a": lambda: 1, "b": lambda: "two"})
        self.assertEqual(got, {"a": 1, "b": "two"})

    def test_the_jobs_actually_run_at_the_same_time(self):
        """Three jobs that each wait on the other two. Run in a row this
        deadlocks until the timeout and the barrier is never crossed; run
        together it passes at once - which is the whole point of the change."""
        gate = threading.Barrier(3, timeout=5)

        def wait():
            gate.wait()
            return True

        began = time.monotonic()
        got = fetch.gather({str(n): wait for n in range(3)})
        self.assertEqual(list(got.values()), [True, True, True])
        self.assertLess(time.monotonic() - began, 5)

    def test_a_failure_comes_back_out_on_the_calling_thread(self):
        def bad():
            raise fetch.SteamError("@err.private")

        with self.assertRaises(fetch.SteamError):
            fetch.gather({"bad": bad, "fine": lambda: 1})

    def test_the_first_job_written_is_the_failure_the_visitor_sees(self):
        """Two bad calls, and which message comes out cannot depend on which
        thread finished first: a private profile has to keep reporting what it
        reported when these ran in a row."""
        def first():
            time.sleep(0.05)
            raise fetch.SteamError("@err.private")

        def second():
            raise fetch.SteamError("@err.refused")

        with self.assertRaises(fetch.SteamError) as caught:
            fetch.gather({"first": first, "second": second})
        self.assertEqual(str(caught.exception), "@err.private")

    def test_everything_else_still_finishes_when_one_fails(self):
        """The other requests are already sent by the time a failure surfaces,
        so they are waited on rather than abandoned half-read."""
        done = threading.Event()

        def slow():
            time.sleep(0.05)
            done.set()

        with self.assertRaises(RuntimeError):
            fetch.gather({"bad": lambda: (_ for _ in ()).throw(RuntimeError("no")),
                          "slow": slow})
        self.assertTrue(done.is_set())

    def test_one_thread_is_a_supported_setting(self):
        """FETCH_FANOUT=1 is the switch for asking whether concurrency is the
        problem, so the serial path has to answer the same thing."""
        was = fetch.FANOUT
        fetch.FANOUT = 1
        try:
            order = []
            fetch.gather({"a": lambda: order.append("a"),
                          "b": lambda: order.append("b")})
            self.assertEqual(order, ["a", "b"])
        finally:
            fetch.FANOUT = was


if __name__ == "__main__":
    unittest.main()
