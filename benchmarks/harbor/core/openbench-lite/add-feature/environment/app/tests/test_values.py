import unittest

from miniconf.values import coerce


class ValuesTest(unittest.TestCase):
    def test_bool(self):
        self.assertIs(coerce("true"), True)
        self.assertIs(coerce("False"), False)

    def test_numbers(self):
        self.assertEqual(coerce("42"), 42)
        self.assertEqual(coerce("3.5"), 3.5)

    def test_strings(self):
        self.assertEqual(coerce("hello"), "hello")
        self.assertEqual(coerce('"quoted"'), "quoted")


if __name__ == "__main__":
    unittest.main()
