"""End-to-end pipeline tests.

These drive whole pipelines through :func:`taskflow.runner.run_pipeline` and
assert on observable outcomes -- final states, attempt counts, run order, and
the event-sourced history. Unlike the focused unit tests, most of these only
pass once *several* interacting defects are fixed, so they encode the
long-horizon difficulty of the task: a partial fix leaves them red.
"""

import unittest

from taskflow.model import State
from taskflow.runner import run_pipeline


def _noop(ctx):
    return None


def _always_fail(ctx):
    raise RuntimeError("boom")


def _flaky(fail_times):
    """An action that fails its first ``fail_times`` attempts, then succeeds."""

    def action(ctx):
        if ctx.attempt <= fail_times:
            raise RuntimeError("transient failure #{}".format(ctx.attempt))

    return action


class PipelineTest(unittest.TestCase):
    # -- retry pipelines (needs the config deep-merge AND the retry boundary) --

    def test_retry_succeeds_on_third_attempt(self):
        # The task overrides the single-attempt default with a budget of three
        # and fails twice before succeeding. This needs the per-task retry
        # override to survive the deep-merge *and* the retry boundary to permit
        # the third attempt.
        report = run_pipeline(
            {
                "name": "retry",
                "defaults": {
                    "retry": {"max_attempts": 1, "backoff": "constant", "base": 0}
                },
                "tasks": [
                    {
                        "id": "flaky",
                        "action": _flaky(2),
                        "retry": {"max_attempts": 3},
                    }
                ],
            }
        )
        self.assertEqual(report.state_of("flaky"), State.SUCCEEDED)
        self.assertEqual(report.attempts_of("flaky"), 3)

    def test_retry_exhausts_to_permanent_failure(self):
        # Five failures against a three-attempt budget: it makes exactly three
        # attempts and then fails permanently.
        report = run_pipeline(
            {
                "name": "retry-exhaust",
                "defaults": {
                    "retry": {"max_attempts": 1, "backoff": "constant", "base": 0}
                },
                "tasks": [
                    {
                        "id": "doomed",
                        "action": _flaky(5),
                        "retry": {"max_attempts": 3},
                    }
                ],
            }
        )
        self.assertEqual(report.state_of("doomed"), State.FAILED)
        self.assertEqual(report.attempts_of("doomed"), 3)

    def test_retry_unblocks_downstream(self):
        # A flaky task recovers on its second attempt, which must let its
        # downstream dependant run to success rather than be skipped.
        report = run_pipeline(
            {
                "name": "retry-chain",
                "defaults": {
                    "retry": {"max_attempts": 1, "backoff": "constant", "base": 0}
                },
                "tasks": [
                    {
                        "id": "flaky",
                        "action": _flaky(1),
                        "retry": {"max_attempts": 2},
                    },
                    {"id": "after", "action": _noop, "deps": ["flaky"]},
                ],
            }
        )
        self.assertEqual(report.state_of("flaky"), State.SUCCEEDED)
        self.assertEqual(report.state_of("after"), State.SUCCEEDED)
        self.assertEqual(report.attempts_of("flaky"), 2)

    # -- priority pipelines (needs the queue tie-break AND the batch guard) ---

    def test_priority_dispatch_order(self):
        # Three tasks become ready together; they must dispatch highest-priority
        # first even though they are declared low-to-high.
        report = run_pipeline(
            {
                "name": "prio",
                "concurrency": 5,
                "tasks": [
                    {"id": "low", "action": _noop, "priority": 1},
                    {"id": "mid", "action": _noop, "priority": 2},
                    {"id": "high", "action": _noop, "priority": 9},
                ],
            }
        )
        self.assertTrue(report.ok())
        self.assertEqual(report.run_order(), ["high", "mid", "low"])

    def test_priority_four_way_batch(self):
        # Four independent tasks with mixed priorities dispatch in strict
        # descending-priority order within the batch.
        report = run_pipeline(
            {
                "name": "prio4",
                "concurrency": 5,
                "tasks": [
                    {"id": "a", "action": _noop, "priority": 1},
                    {"id": "b", "action": _noop, "priority": 3},
                    {"id": "c", "action": _noop, "priority": 2},
                    {"id": "d", "action": _noop, "priority": 5},
                ],
            }
        )
        self.assertTrue(report.ok())
        self.assertEqual(report.run_order(), ["d", "b", "c", "a"])

    def test_priority_respected_after_dependency(self):
        # Once a root finishes, its two dependants become ready together and
        # must run high-priority first.
        report = run_pipeline(
            {
                "name": "prio-dep",
                "concurrency": 5,
                "tasks": [
                    {"id": "root", "action": _noop},
                    {"id": "cheap", "action": _noop, "deps": ["root"], "priority": 1},
                    {"id": "urgent", "action": _noop, "deps": ["root"], "priority": 8},
                ],
            }
        )
        self.assertTrue(report.ok())
        self.assertEqual(report.run_order(), ["root", "urgent", "cheap"])

    # -- resource pipelines (needs the release-by-cost fix) ------------------

    def test_resource_serialized_run_all_finish(self):
        # Each task needs the whole pool, so they run strictly one at a time.
        # Every task must still finish -- which only happens if each release
        # returns the *full* cost so the next task can be admitted.
        report = run_pipeline(
            {
                "name": "res-serial",
                "pools": {"db": 2},
                "concurrency": 5,
                "tasks": [
                    {"id": "t1", "action": _noop, "resources": {"db": 2}},
                    {"id": "t2", "action": _noop, "resources": {"db": 2}},
                    {"id": "t3", "action": _noop, "resources": {"db": 2}},
                ],
            }
        )
        self.assertTrue(report.ok())
        for tid in ("t1", "t2", "t3"):
            self.assertEqual(report.state_of(tid), State.SUCCEEDED)

    def test_resource_peak_and_completion(self):
        # Four costly tasks share a pool sized for one at a time; the peak usage
        # equals a single task's cost and all four complete.
        report = run_pipeline(
            {
                "name": "res-peak",
                "pools": {"mem": 2},
                "concurrency": 5,
                "tasks": [
                    {"id": "w1", "action": _noop, "resources": {"mem": 2}},
                    {"id": "w2", "action": _noop, "resources": {"mem": 2}},
                    {"id": "w3", "action": _noop, "resources": {"mem": 2}},
                    {"id": "w4", "action": _noop, "resources": {"mem": 2}},
                ],
            }
        )
        self.assertTrue(report.ok())
        self.assertEqual(report.history.peak_resource_usage("mem"), 2)

    # -- transitive-skip cascades (needs the scheduler transitive fix) -------

    def test_transitive_skip_chain(self):
        # a fails permanently; the whole downstream chain must be SKIPPED, not
        # just a's direct child.
        report = run_pipeline(
            {
                "name": "skip-chain",
                "tasks": [
                    {"id": "a", "action": _always_fail},
                    {"id": "b", "action": _noop, "deps": ["a"]},
                    {"id": "c", "action": _noop, "deps": ["b"]},
                    {"id": "d", "action": _noop, "deps": ["c"]},
                ],
            }
        )
        self.assertEqual(report.state_of("a"), State.FAILED)
        for tid in ("b", "c", "d"):
            self.assertEqual(report.state_of(tid), State.SKIPPED)

    def test_diamond_transitive_skip(self):
        # a fails; both branches of the diamond and the join must be skipped.
        report = run_pipeline(
            {
                "name": "skip-diamond",
                "tasks": [
                    {"id": "a", "action": _always_fail},
                    {"id": "b", "action": _noop, "deps": ["a"]},
                    {"id": "c", "action": _noop, "deps": ["a"]},
                    {"id": "d", "action": _noop, "deps": ["b", "c"]},
                ],
            }
        )
        self.assertEqual(report.state_of("a"), State.FAILED)
        self.assertEqual(sorted(report.skipped()), ["b", "c", "d"])

    # -- history / event outcomes (needs the prefix-match fix, some + skip) --

    def test_history_terminal_events_delivered(self):
        # A single failing task must surface in the terminal-outcome history,
        # which is fed by a *prefix* subscription on ``job.term``.
        report = run_pipeline(
            {"name": "term", "tasks": [{"id": "boom", "action": _always_fail}]}
        )
        self.assertEqual(report.history.failed_jobs(), ["boom"])
        self.assertEqual(
            report.history.outcome_counts(),
            {"succeeded": 0, "failed": 1, "skipped": 0},
        )

    def test_history_failed_and_skipped_sets(self):
        report = run_pipeline(
            {
                "name": "mixed",
                "tasks": [
                    {"id": "ok", "action": _noop},
                    {"id": "bad", "action": _always_fail},
                    {"id": "mid", "action": _noop, "deps": ["bad"]},
                    {"id": "leaf", "action": _noop, "deps": ["mid"]},
                ],
            }
        )
        self.assertEqual(report.history.failed_jobs(), ["bad"])
        self.assertEqual(report.history.skipped_jobs(), ["mid", "leaf"])
        self.assertEqual(report.history.succeeded_jobs(), ["ok"])

    def test_history_outcome_counts_mixed(self):
        report = run_pipeline(
            {
                "name": "counts",
                "tasks": [
                    {"id": "a", "action": _noop},
                    {"id": "bad", "action": _always_fail},
                    {"id": "down", "action": _noop, "deps": ["bad"]},
                    {"id": "down2", "action": _noop, "deps": ["down"]},
                ],
            }
        )
        self.assertEqual(
            report.history.outcome_counts(),
            {"succeeded": 1, "failed": 1, "skipped": 2},
        )

    # -- steep combined pipeline (needs retry + priority + resources fixed) --

    def test_full_pipeline_retry_priority_resources(self):
        # A pipeline that only reaches a fully-green outcome when the config
        # merge, retry boundary, queue ordering, batch guard, and resource
        # release all behave correctly at once.
        report = run_pipeline(
            {
                "name": "full",
                "pools": {"cpu": 2},
                "concurrency": 5,
                "defaults": {
                    "retry": {"max_attempts": 1, "backoff": "constant", "base": 0}
                },
                "tasks": [
                    {"id": "seed", "action": _noop, "priority": 5},
                    {
                        "id": "flaky",
                        "action": _flaky(1),
                        "deps": ["seed"],
                        "retry": {"max_attempts": 2},
                        "priority": 3,
                    },
                    {"id": "hi", "action": _noop, "priority": 9, "resources": {"cpu": 2}},
                    {"id": "lo", "action": _noop, "priority": 1, "resources": {"cpu": 2}},
                    {"id": "sink", "action": _noop, "deps": ["flaky", "hi", "lo"]},
                ],
            }
        )
        self.assertTrue(report.ok())
        self.assertEqual(report.attempts_of("flaky"), 2)
        self.assertEqual(report.state_of("sink"), State.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
