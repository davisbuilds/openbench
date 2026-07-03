import unittest

from miniconf.errors import ConfigError
from miniconf.parser import parse_lines


class ParserTest(unittest.TestCase):
    def test_sections_and_keys(self):
        cfg = parse_lines([
            "name = app",
            "[db]",
            "host = localhost",
            "port = 5432",
        ])
        self.assertEqual(cfg.get("name"), "app")
        self.assertEqual(cfg.get("host", section="db"), "localhost")
        self.assertEqual(cfg.get("port", section="db"), 5432)

    def test_comments_and_blanks_ignored(self):
        cfg = parse_lines([
            "# a comment",
            "",
            "key = value",
        ])
        self.assertEqual(cfg.get("key"), "value")
        self.assertEqual(cfg.sections(), [])

    def test_later_key_overrides(self):
        cfg = parse_lines(["k = 1", "k = 2"])
        self.assertEqual(cfg.get("k"), 2)

    def test_malformed_line_raises(self):
        with self.assertRaises(ConfigError):
            parse_lines(["this is not valid"])

    def test_unknown_directive_ignored(self):
        cfg = parse_lines(["@debug on", "k = 1"])
        self.assertEqual(cfg.get("k"), 1)


if __name__ == "__main__":
    unittest.main()
