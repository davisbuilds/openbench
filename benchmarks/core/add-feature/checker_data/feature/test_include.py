import os
import tempfile
import unittest

from miniconf.errors import ConfigError
from miniconf.loader import load


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class IncludeTest(unittest.TestCase):
    def test_basic_include(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "base.conf"), "shared = 1\n")
            _write(os.path.join(d, "main.conf"),
                   "@include base.conf\nlocal = 2\n")
            cfg = load(os.path.join(d, "main.conf"))
            self.assertEqual(cfg.get("shared"), 1)
            self.assertEqual(cfg.get("local"), 2)

    def test_nested_include(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "c.conf"), "c = 3\n")
            _write(os.path.join(d, "b.conf"), "@include c.conf\nb = 2\n")
            _write(os.path.join(d, "a.conf"), "@include b.conf\na = 1\n")
            cfg = load(os.path.join(d, "a.conf"))
            self.assertEqual(cfg.get("a"), 1)
            self.assertEqual(cfg.get("b"), 2)
            self.assertEqual(cfg.get("c"), 3)

    def test_relative_to_including_file(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "sub", "inner.conf"), "deep = 9\n")
            _write(os.path.join(d, "sub", "mid.conf"),
                   "@include inner.conf\n")
            _write(os.path.join(d, "top.conf"), "@include sub/mid.conf\n")
            cfg = load(os.path.join(d, "top.conf"))
            self.assertEqual(cfg.get("deep"), 9)

    def test_override_after_include(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "base.conf"),
                   "port = 1000\nfrom_base = yes\n")
            _write(os.path.join(d, "main.conf"),
                   "@include base.conf\nport = 2000\n")
            cfg = load(os.path.join(d, "main.conf"))
            # The included key is present (proving expansion happened) and the
            # later assignment wins over the included one.
            self.assertEqual(cfg.get("from_base"), "yes")
            self.assertEqual(cfg.get("port"), 2000)

    def test_missing_include_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "main.conf"), "@include nope.conf\n")
            with self.assertRaises(ConfigError):
                load(os.path.join(d, "main.conf"))

    def test_cycle_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a.conf"), "@include b.conf\n")
            _write(os.path.join(d, "b.conf"), "@include a.conf\n")
            with self.assertRaises(ConfigError):
                load(os.path.join(d, "a.conf"))


if __name__ == "__main__":
    unittest.main()
