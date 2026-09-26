import unittest

from taskflow.retry import CONSTANT, RetryPolicy


class RetryTest(unittest.TestCase):
    def test_constant_backoff(self):
        policy = RetryPolicy(max_attempts=5, backoff=CONSTANT, base=3)
        self.assertEqual(policy.next_delay(1), 3)
        self.assertEqual(policy.next_delay(2), 3)
        self.assertEqual(policy.next_delay(4), 3)

    def test_should_retry_boundary(self):
        # With three attempts allowed, the run may retry after the first and
        # second attempts, but the third failure is permanent.
        policy = RetryPolicy(max_attempts=3)
        self.assertTrue(policy.should_retry(1))
        self.assertTrue(policy.should_retry(2))
        self.assertFalse(policy.should_retry(3))
        self.assertFalse(policy.should_retry(4))


if __name__ == "__main__":
    unittest.main()
