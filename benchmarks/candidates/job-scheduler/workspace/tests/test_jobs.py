import unittest

from scheduler.jobs import Job, Status


class JobsTest(unittest.TestCase):
    def test_defaults(self):
        job = Job("j1")
        self.assertEqual(job.status, Status.PENDING)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.deps, [])
        self.assertFalse(job.is_terminal())

    def test_job_metadata(self):
        job = Job("j2", deps=["a", "b"], priority=3, max_retries=2, cost=4)
        self.assertEqual(job.deps, ["a", "b"])
        self.assertEqual(job.priority, 3)
        self.assertEqual(job.max_retries, 2)
        self.assertEqual(job.cost, 4)


if __name__ == "__main__":
    unittest.main()
