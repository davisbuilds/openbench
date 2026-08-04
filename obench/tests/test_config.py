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

    def test_requires_complete_harbor_suite_runtime_config(self):
        root = os.path.join(self.tmp, "project")
        nested = os.path.join(root, "src")
        os.makedirs(nested)
        openbench = os.path.join(root, ".openbench")
        os.makedirs(openbench)
        with open(
            os.path.join(openbench, "openbench.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                'default_suite = ".openbench/suites/default.toml"\n'
                'jobs_dir = ".openbench/jobs"\n'
                'results_dir = ".openbench/results"\n'
                'trajectories_dir = ".openbench/trajectories"\n'
            )
        cfg = config.require_suite_config(nested)
        self.assertEqual(
            cfg.default_suite,
            os.path.join(root, ".openbench", "suites", "default.toml"),
        )

        with open(
            os.path.join(openbench, "openbench.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write('default_suite = ".openbench/suites/default.toml"\n')
        with self.assertRaisesRegex(ValueError, "missing required suite settings"):
            config.require_suite_config(nested)

    def test_suite_runtime_config_rejects_output_symlink_escape(self):
        root = os.path.join(self.tmp, "project")
        nested = os.path.join(root, "src")
        openbench = os.path.join(root, ".openbench")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(nested)
        os.makedirs(outside)
        os.makedirs(os.path.join(openbench, "suites"))
        with open(
            os.path.join(openbench, "suites", "default.toml"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("schema_version = 1\n")
        os.symlink(outside, os.path.join(openbench, "results"))
        with open(
            os.path.join(openbench, "openbench.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                'default_suite = ".openbench/suites/default.toml"\n'
                'jobs_dir = ".openbench/jobs"\n'
                'results_dir = ".openbench/results"\n'
                'trajectories_dir = ".openbench/trajectories"\n'
            )

        with self.assertRaisesRegex(ValueError, "results_dir.*symlink"):
            config.require_suite_config(nested)
        self.assertEqual(os.listdir(outside), [])


if __name__ == "__main__":
    unittest.main()
