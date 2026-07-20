#!/usr/bin/env python3
"""Tests for openbench.toml config discovery."""

import os
import shutil
import tempfile
import unittest

from obench import config, paths


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_config_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_load_config_from_ancestor(self):
        root = os.path.join(self.tmp, "proj")
        nested = os.path.join(root, "src", "app")
        os.makedirs(nested)
        ob = os.path.join(root, ".openbench")
        os.makedirs(ob)
        with open(os.path.join(ob, "openbench.toml"), "w", encoding="utf-8") as fh:
            fh.write(
                'tasks_dir = ".openbench/tasks"\n'
                'results_path = ".openbench/results/results.jsonl"\n'
                'harnesses = ["null", "pi"]\n'
                'model = "glm-4.7-flash"\n'
                "trials = 3\n"
            )
        os.makedirs(os.path.join(ob, "tasks"))

        cfg = config.load_config(nested)
        self.assertEqual(cfg.project_root, os.path.abspath(root))
        self.assertEqual(cfg.tasks_dir, os.path.join(root, ".openbench", "tasks"))
        self.assertEqual(
            cfg.results_path,
            os.path.join(root, ".openbench", "results", "results.jsonl"),
        )
        self.assertEqual(cfg.harnesses, ["null", "pi"])
        self.assertEqual(cfg.model, "glm-4.7-flash")
        self.assertEqual(cfg.trials, 3)

        self.assertEqual(
            paths.default_results_path(nested),
            cfg.results_path,
        )
        self.assertEqual(paths.default_tasks_dir(nested), cfg.tasks_dir)

    def test_missing_config_is_empty(self):
        cfg = config.load_config(self.tmp)
        self.assertIsNone(cfg.path)
        self.assertIsNone(cfg.tasks_dir)
        self.assertEqual(cfg.harnesses, [])


if __name__ == "__main__":
    unittest.main()
