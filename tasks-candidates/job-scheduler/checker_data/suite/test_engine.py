import unittest

from scheduler.engine import Engine
from scheduler.jobs import Job, Status
from scheduler.resources import ResourcePool


def boom():
    raise RuntimeError("boom")


class Flaky:
    """A callable that raises ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("transient")


class EngineTest(unittest.TestCase):
    def test_linear_dependency_order(self):
        # b depends on a; a must actually run before b.
        order = []
        a = Job("a", run=lambda: order.append("a"))
        b = Job("b", run=lambda: order.append("b"), deps=["a"])
        Engine([b, a]).run()
        self.assertEqual(order, ["a", "b"])
        self.assertEqual(a.status, Status.SUCCEEDED)
        self.assertEqual(b.status, Status.SUCCEEDED)

    def test_succeeds_on_last_retry(self):
        # Fails twice then succeeds; with max_retries=2 the third (final) attempt
        # must be allowed, so the job succeeds after 3 runs.
        flaky = Flaky(fail_times=2)
        job = Job("j", run=flaky, max_retries=2)
        Engine([job]).run()
        self.assertEqual(job.status, Status.SUCCEEDED)
        self.assertEqual(job.attempts, 3)
        self.assertEqual(flaky.calls, 3)

    def test_downstream_skipped_on_permanent_failure(self):
        # a fails with no retries; its dependent b must be SKIPPED, not run, not
        # failed.
        ran_b = []
        a = Job("a", run=boom, max_retries=0)
        b = Job("b", run=lambda: ran_b.append(True), deps=["a"])
        Engine([a, b]).run()
        self.assertEqual(a.status, Status.FAILED)
        self.assertEqual(b.status, Status.SKIPPED)
        self.assertEqual(ran_b, [])

    def test_transitive_dependents_skipped(self):
        # a -> b -> c: a fails, so both b and c must end SKIPPED.
        a = Job("a", run=boom)
        b = Job("b", deps=["a"])
        c = Job("c", deps=["b"])
        Engine([a, b, c]).run()
        self.assertEqual(a.status, Status.FAILED)
        self.assertEqual(b.status, Status.SKIPPED)
        self.assertEqual(c.status, Status.SKIPPED)

    def test_pooled_jobs_all_complete(self):
        # Two independent jobs, each costing the full width of a capacity-2 pool,
        # run one at a time. The pool must be fully released after the first so
        # the second can be admitted; both must succeed.
        pool = ResourcePool(2)
        first = Job("first", priority=10, cost=2)
        second = Job("second", priority=1, cost=2)
        Engine([first, second], concurrency=1, pool=pool).run()
        self.assertEqual(first.status, Status.SUCCEEDED)
        self.assertEqual(second.status, Status.SUCCEEDED)
        self.assertEqual(pool.available(), 2)


if __name__ == "__main__":
    unittest.main()
