import unittest

from catalog.fees import late_fee


class FeesTest(unittest.TestCase):
    def test_no_fee_when_on_time(self):
        self.assertEqual(late_fee(0), 0.0)
        self.assertEqual(late_fee(-2), 0.0)

    def test_one_day_late(self):
        self.assertEqual(late_fee(1), 0.25)

    def test_several_days_late(self):
        self.assertEqual(late_fee(4), 1.00)


if __name__ == "__main__":
    unittest.main()
