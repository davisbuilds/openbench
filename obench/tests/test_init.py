#!/usr/bin/env python3
"""Tests for obench init scaffolding."""

import os
import shutil
import tempfile
import unittest

from obench import init


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_init_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._cwd)

    def test_init_creates_scaffold_idempotently(self):
        notes1 = init.init_scaffold()
        self.assertTrue(any("created:" in n for n in notes1))
        root = os.path.join(self.tmp, ".openbench")
        self.assertTrue(os.path.isfile(os.path.join(root, "openbench.toml")))
        self.assertTrue(os.path.isfile(os.path.join(root, ".gitignore")))
        self.assertTrue(os.path.isdir(os.path.join(root, "tasks", "example")))
        self.assertTrue(os.path.isfile(
            os.path.join(root, "tasks", "example", "solution", "greeting.txt")))

        notes2 = init.init_scaffold()
        self.assertTrue(all("skip (exists)" in n for n in notes2))

    def test_init_task_from_dir(self):
        seed = os.path.join(self.tmp, "seed")
        os.makedirs(seed)
        with open(os.path.join(seed, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")

        path = init.init_task("demo", from_dir=seed)
        self.assertTrue(os.path.isfile(os.path.join(path, "workspace", "app.py")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, ".openbench", "openbench.toml")))

    def test_cli_init_task_requires_from_pairing(self):
        with self.assertRaises(SystemExit) as ctx:
            init.main(["--from", "x"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
