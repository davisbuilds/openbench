"""Tests for the new {% for %} loop feature.

These fail on the untouched workspace (loops are not implemented) and pass once
the feature is added: simple iteration, empty and absent iterables, dotted item
lookups, nested loops, and a loop containing an if.
"""
import unittest

from template import render


class LoopTest(unittest.TestCase):
    def test_for_simple(self):
        self.assertEqual(
            render("{% for n in nums %}{{ n }},{% endfor %}", {"nums": [1, 2, 3]}),
            "1,2,3,",
        )

    def test_for_empty_iterable(self):
        self.assertEqual(
            render("a{% for n in nums %}{{ n }}{% endfor %}b", {"nums": []}), "ab"
        )

    def test_for_absent_iterable(self):
        self.assertEqual(
            render("a{% for n in nums %}x{% endfor %}b", {}), "ab"
        )

    def test_for_dotted_item(self):
        ctx = {"users": [{"name": "Ann"}, {"name": "Bo"}]}
        self.assertEqual(
            render("{% for u in users %}{{ u.name }} {% endfor %}", ctx), "Ann Bo "
        )

    def test_for_nested(self):
        tmpl = "{% for r in rows %}{% for c in r %}{{ c }}{% endfor %}|{% endfor %}"
        self.assertEqual(render(tmpl, {"rows": [[1, 2], [3, 4]]}), "12|34|")

    def test_for_with_if(self):
        tmpl = "{% for n in nums %}{% if n %}{{ n }}{% endif %}{% endfor %}"
        self.assertEqual(render(tmpl, {"nums": [0, 1, 2, 0, 3]}), "123")


if __name__ == "__main__":
    unittest.main()
