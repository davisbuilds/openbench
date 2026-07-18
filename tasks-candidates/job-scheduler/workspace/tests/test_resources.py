import unittest

from scheduler.resources import ResourcePool, ResourceError


class ResourcesTest(unittest.TestCase):
    def test_capacity_guard(self):
        # Must never admit beyond capacity.
        pool = ResourcePool(3)
        pool.acquire(2)
        self.assertTrue(pool.can_admit(1))
        self.assertFalse(pool.can_admit(2))
        with self.assertRaises(ResourceError):
            pool.acquire(2)

    def test_release_frees_capacity(self):
        pool = ResourcePool(2)
        pool.acquire(1)
        self.assertEqual(pool.available(), 1)
        pool.release(1)
        self.assertEqual(pool.available(), 2)

    def test_cannot_over_admit_sequence(self):
        # A full pool rejects further admission until something is released.
        pool = ResourcePool(2)
        pool.acquire(1)
        pool.acquire(1)
        self.assertEqual(pool.available(), 0)
        self.assertFalse(pool.can_admit(1))
        pool.release(1)
        self.assertTrue(pool.can_admit(1))


if __name__ == "__main__":
    unittest.main()
