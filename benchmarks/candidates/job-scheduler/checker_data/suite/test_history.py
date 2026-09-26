import unittest

from scheduler.engine import Engine
from scheduler.history import Event, History
from scheduler.jobs import Job


def boom():
    raise RuntimeError("boom")


class AlwaysFail:
    def __call__(self):
        raise RuntimeError("nope")


class HistoryTest(unittest.TestCase):
    def test_events_in_order(self):
        h = History()
        h.record("a", Event.STARTED)
        h.record("a", Event.SUCCEEDED)
        self.assertEqual(h.events(),
                         [("a", Event.STARTED), ("a", Event.SUCCEEDED)])

    def test_run_order_reflects_priority(self):
        # "z" has the higher priority so it runs first, even though its id sorts
        # after "a". run_order must reflect execution order, not id order.
        z = Job("z", priority=10)
        a = Job("a", priority=1)
        history = Engine([z, a], concurrency=1).run()
        self.assertEqual(history.run_order(), ["z", "a"])

    def test_retries_for_counts(self):
        # Always fails with max_retries=2: attempts 1, 2, 3 -> two retries logged
        # before the final permanent failure.
        job = Job("j", run=AlwaysFail(), max_retries=2)
        history = Engine([job]).run()
        self.assertEqual(history.retries_for("j"), 2)

    def test_skipped_jobs_listed(self):
        # a fails, b depends on a, so b is skipped and must appear in the log.
        a = Job("a", run=boom)
        b = Job("b", deps=["a"])
        history = Engine([a, b]).run()
        self.assertEqual(history.skipped_jobs(), ["b"])


if __name__ == "__main__":
    unittest.main()
