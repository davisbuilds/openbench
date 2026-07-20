#!/usr/bin/env python3
"""Tests for bench/add_task.py."""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import add_task  # noqa: E402


class AddTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_add_task_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scaffold_structure_and_templates(self):
        task = os.path.join(self.tmp, "tasks", "new-task")
        created = add_task.scaffold(task)

        self.assertEqual(created, os.path.abspath(task))
        for rel in ("instruction.md", "workspace", "solution", "checker_data", "checker.sh", "PROVENANCE.md"):
            self.assertTrue(os.path.exists(os.path.join(task, rel)), rel)

        with open(os.path.join(task, "instruction.md"), encoding="utf-8") as fh:
            self.assertIn("TODO", fh.read())
        with open(os.path.join(task, "PROVENANCE.md"), encoding="utf-8") as fh:
            provenance = fh.read()
        self.assertIn("Source: TODO", provenance)
        self.assertIn("Author: TODO", provenance)
        self.assertIn("Date:", provenance)

        checker = os.path.join(task, "checker.sh")
        mode = os.stat(checker).st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        with open(checker, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("set -eu", text)
        self.assertIn("TASK_DIR", text)
        self.assertIn("PASS:", text)
        self.assertIn("FAIL:", text)
        self.assertIn("SCORE:", text)

    def test_from_copies_starting_workspace(self):
        source = os.path.join(self.tmp, "source")
        os.makedirs(os.path.join(source, "nested"))
        with open(os.path.join(source, "nested", "seed.txt"), "w", encoding="utf-8") as fh:
            fh.write("seed\n")

        task = os.path.join(self.tmp, "tasks", "copied")
        add_task.scaffold(task, from_dir=source)

        with open(os.path.join(task, "workspace", "nested", "seed.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "seed\n")

    def test_from_rejects_symlinks(self):
        source = os.path.join(self.tmp, "source")
        os.makedirs(os.path.join(source, "nested"))
        os.symlink("nested", os.path.join(source, "nested-link"))

        task = os.path.join(self.tmp, "tasks", "symlinked")
        with self.assertRaises(ValueError):
            add_task.scaffold(task, from_dir=source)
        self.assertFalse(os.path.exists(task))

    def test_from_rejects_symlinks_that_escape_source_tree(self):
        source = os.path.join(self.tmp, "source")
        outside = os.path.join(self.tmp, "outside.txt")
        os.makedirs(source)
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("do not copy contents\n")
        os.symlink(outside, os.path.join(source, "outside-link.txt"))

        task = os.path.join(self.tmp, "tasks", "symlinked")
        with self.assertRaises(ValueError):
            add_task.scaffold(task, from_dir=source)
        self.assertFalse(os.path.exists(task))

    def test_from_rejects_task_inside_source_tree(self):
        source = os.path.join(self.tmp, "source")
        os.makedirs(source)
        task = os.path.join(source, "new-task")

        with self.assertRaises(ValueError):
            add_task.scaffold(task, from_dir=source)
        self.assertFalse(os.path.exists(task))

    def test_refuses_existing_task_dir(self):
        task = os.path.join(self.tmp, "tasks", "exists")
        os.makedirs(task)
        with self.assertRaises(FileExistsError):
            add_task.scaffold(task)

    def test_gate_flag_runs_gate_and_returns_verdict(self):
        task = os.path.join(self.tmp, "tasks", "gate-me")
        fake_result = {"status": "PASS", "task": os.path.abspath(task), "findings": []}
        with mock.patch.object(add_task.admission_gate, "gate", return_value=fake_result) as gate, \
                mock.patch.object(add_task.admission_gate, "print_human") as print_human:
            code = add_task.main([task, "--gate"])
        self.assertEqual(code, 0)
        gate.assert_called_once_with(os.path.abspath(task))
        print_human.assert_called_once_with(fake_result)


if __name__ == "__main__":
    unittest.main()
