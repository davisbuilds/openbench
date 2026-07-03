import unittest

from taskflow.config import deep_merge, load_pipeline


def _noop(ctx):
    return None


class ConfigTest(unittest.TestCase):
    def test_load_basic(self):
        pipe = load_pipeline(
            {
                "name": "etl",
                "tasks": [
                    {"id": "a", "action": _noop},
                    {"id": "b", "action": _noop, "deps": ["a"]},
                ],
            }
        )
        self.assertEqual(pipe.name, "etl")
        self.assertEqual(pipe.task_ids(), ["a", "b"])
        self.assertEqual(pipe.get("b").deps, ["a"])

    def test_deep_merge_direct(self):
        merged = deep_merge(
            {"retry": {"max_attempts": 1, "backoff": "constant"}, "priority": 0},
            {"retry": {"max_attempts": 3}, "priority": 9},
        )
        self.assertEqual(merged["retry"]["max_attempts"], 3)
        self.assertEqual(merged["retry"]["backoff"], "constant")
        self.assertEqual(merged["priority"], 9)

    def test_task_retry_override_preserved(self):
        # A task setting only retry.max_attempts must keep its own value while
        # still inheriting the default backoff from the deep-merge.
        pipe = load_pipeline(
            {
                "defaults": {
                    "priority": 0,
                    "retry": {"max_attempts": 1, "backoff": "constant"},
                },
                "tasks": [
                    {
                        "id": "flaky",
                        "action": _noop,
                        "priority": 7,
                        "retry": {"max_attempts": 5},
                    }
                ],
            }
        )
        task = pipe.get("flaky")
        self.assertEqual(task.retry_policy.max_attempts, 5)
        # Top-level overrides and inherited nested keys both survive.
        self.assertEqual(task.priority, 7)
        self.assertEqual(task.retry_policy.backoff, "constant")


if __name__ == "__main__":
    unittest.main()
