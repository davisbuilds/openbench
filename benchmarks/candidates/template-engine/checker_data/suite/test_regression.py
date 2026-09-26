"""Regression tests for the pre-existing engine features.

These must pass on the untouched workspace AND after the new features are added:
literal text, dotted variable lookup (dict and attribute), and if/else
conditionals including nested ifs.
"""
import types
import unittest

from template import render


class RegressionTest(unittest.TestCase):
    def test_literal_text(self):
        self.assertEqual(render("Hello, world!", {}), "Hello, world!")

    def test_simple_var(self):
        self.assertEqual(render("Hi {{ name }}!", {"name": "Ann"}), "Hi Ann!")

    def test_dotted_dict_lookup(self):
        self.assertEqual(
            render("{{ user.name }}", {"user": {"name": "Ann"}}), "Ann"
        )

    def test_dotted_attr_lookup(self):
        user = types.SimpleNamespace(name="Bo")
        self.assertEqual(render("{{ user.name }}", {"user": user}), "Bo")

    def test_missing_var_empty(self):
        self.assertEqual(render("[{{ nope }}]", {}), "[]")

    def test_if_true(self):
        self.assertEqual(render("{% if ok %}yes{% endif %}", {"ok": True}), "yes")

    def test_if_false_else(self):
        self.assertEqual(
            render("{% if ok %}yes{% else %}no{% endif %}", {"ok": False}), "no"
        )

    def test_if_no_else_false(self):
        self.assertEqual(
            render("a{% if ok %}yes{% endif %}b", {"ok": False}), "ab"
        )

    def test_nested_if(self):
        tmpl = "{% if a %}{% if b %}AB{% else %}A{% endif %}{% endif %}"
        self.assertEqual(render(tmpl, {"a": True, "b": False}), "A")


if __name__ == "__main__":
    unittest.main()
