import unittest

from catalog.books import parse_book


class BooksTest(unittest.TestCase):
    def test_parse_fields(self):
        b = parse_book("Dune | Herbert | 1965 | 3")
        self.assertEqual(b.title, "Dune")
        self.assertEqual(b.author, "Herbert")
        self.assertEqual(b.copies, 3)

    def test_year_is_int(self):
        b = parse_book("Dune | Herbert | 1965 | 3")
        self.assertEqual(b.year, 1965)

    def test_age(self):
        b = parse_book("Dune | Herbert | 1965 | 3")
        self.assertEqual(b.age(2020), 55)


if __name__ == "__main__":
    unittest.main()
