import unittest

from catalog.factory import sample_books


class FactoryTest(unittest.TestCase):
    def test_sample_count(self):
        self.assertEqual(len(sample_books()), 3)

    def test_sample_titles(self):
        titles = sorted(b.title for b in sample_books())
        self.assertEqual(titles, ["1984", "Dune", "Emma"])


if __name__ == "__main__":
    unittest.main()
