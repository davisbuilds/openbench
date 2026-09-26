"""Tests for the new variable filter feature.

These fail on the untouched workspace (filters are not implemented) and pass
once the feature is added: upper, lower, length (string and list), default
(missing / present / empty-string), join, and chaining filters.
"""
import unittest

from template import render


class FilterTest(unittest.TestCase):
    def test_filter_upper(self):
        self.assertEqual(render("{{ name | upper }}", {"name": "dune"}), "DUNE")

    def test_filter_lower(self):
        self.assertEqual(render("{{ name | lower }}", {"name": "DUNE"}), "dune")

    def test_filter_length_string(self):
        self.assertEqual(render("{{ name | length }}", {"name": "dune"}), "4")

    def test_filter_length_list(self):
        self.assertEqual(render("{{ items | length }}", {"items": [1, 2, 3]}), "3")

    def test_filter_default_missing(self):
        self.assertEqual(render('{{ nick | default:"n/a" }}', {}), "n/a")

    def test_filter_default_present(self):
        self.assertEqual(
            render('{{ nick | default:"n/a" }}', {"nick": "Bo"}), "Bo"
        )

    def test_filter_default_empty_string(self):
        self.assertEqual(
            render('{{ nick | default:"n/a" }}', {"nick": ""}), "n/a"
        )

    def test_filter_join(self):
        self.assertEqual(
            render('{{ tags | join:", " }}', {"tags": ["a", "b", "c"]}), "a, b, c"
        )

    def test_filter_chain(self):
        self.assertEqual(
            render('{{ tags | join:"-" | upper }}', {"tags": ["a", "b"]}), "A-B"
        )


if __name__ == "__main__":
    unittest.main()
