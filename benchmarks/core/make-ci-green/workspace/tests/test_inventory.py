import unittest

from catalog.inventory import Inventory


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()

    def test_add_and_available(self):
        self.inv.add("b1", 3)
        self.inv.add("b1", 2)
        self.assertEqual(self.inv.available("b1"), 5)

    def test_checkout_decrements(self):
        self.inv.add("b1", 2)
        self.inv.checkout("b1")
        self.assertEqual(self.inv.available("b1"), 1)

    def test_recent_returns_n_events(self):
        for i in range(5):
            self.inv.add("b{}".format(i), 1)
        recent = self.inv.recent(3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[-1], ("add", "b4", 1))


if __name__ == "__main__":
    unittest.main()
