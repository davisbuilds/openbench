import os
import tempfile
import unittest

from miniconf.loader import load, loads


class LoaderTest(unittest.TestCase):
    def test_loads_string(self):
        cfg = loads("name = app\n[db]\nport = 5432\n")
        self.assertEqual(cfg.get("name"), "app")
        self.assertEqual(cfg.get("port", section="db"), 5432)

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "app.conf")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("greeting = hello\nretries = 3\n")
            cfg = load(path)
            self.assertEqual(cfg.get("greeting"), "hello")
            self.assertEqual(cfg.get("retries"), 3)


if __name__ == "__main__":
    unittest.main()
