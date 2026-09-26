import unittest

from miniconf.config import Config
from miniconf.errors import ConfigError


class ConfigTest(unittest.TestCase):
    def test_get_with_default(self):
        cfg = Config()
        cfg.set(None, "k", 1)
        self.assertEqual(cfg.get("k"), 1)
        self.assertEqual(cfg.get("missing", default="fallback"), "fallback")

    def test_get_missing_raises(self):
        cfg = Config()
        with self.assertRaises(ConfigError):
            cfg.get("nope")

    def test_sections_and_as_dict(self):
        cfg = Config()
        cfg.set(None, "a", 1)
        cfg.set("s", "b", 2)
        self.assertEqual(cfg.sections(), ["s"])
        self.assertEqual(cfg.as_dict(), {None: {"a": 1}, "s": {"b": 2}})


if __name__ == "__main__":
    unittest.main()
