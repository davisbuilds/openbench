import unittest

from catalog.books import Book
from catalog.search import search_by_author, sort_by_year


class SearchTest(unittest.TestCase):
    def _books(self):
        return [
            Book("Dune", "Herbert", 1965, 3),
            Book("Emma", "Austen", 1815, 1),
            Book("Persuasion", "austen", 1817, 1),
        ]

    def test_search_by_author_normalizes(self):
        found = search_by_author(self._books(), " AUSTEN ")
        titles = sorted(b.title for b in found)
        self.assertEqual(titles, ["Emma", "Persuasion"])

    def test_sort_by_year(self):
        ordered = sort_by_year(self._books())
        self.assertEqual([b.year for b in ordered], [1815, 1817, 1965])


if __name__ == "__main__":
    unittest.main()
