import unittest

from taskflow.resources import ResourceManager, ResourcePool


class ResourcePoolTest(unittest.TestCase):
    def test_admit_and_release(self):
        pool = ResourcePool("cpu", 3)
        self.assertTrue(pool.can_admit(1))
        pool.acquire(1)
        self.assertEqual(pool.used, 1)
        self.assertEqual(pool.available(), 2)
        pool.release(1)
        self.assertEqual(pool.used, 0)


class ResourceManagerTest(unittest.TestCase):
    def test_admit_and_release_single(self):
        mgr = ResourceManager({"cpu": 2})
        self.assertTrue(mgr.try_admit({"cpu": 1}))
        self.assertEqual(mgr.usage("cpu"), 1)
        mgr.release({"cpu": 1})
        self.assertEqual(mgr.usage("cpu"), 0)


if __name__ == "__main__":
    unittest.main()
