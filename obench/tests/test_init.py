#!/usr/bin/env python3
"""Tests for Harbor-native ``obench init`` scaffolding."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unittest

from obench import init
from obench.profile_spec import load_profile_registry
from obench.suite import load_suite


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="obench_init_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._cwd)

    def test_init_creates_harbor_native_scaffold_idempotently(self):
        notes1 = init.init_scaffold()
        self.assertTrue(any("created:" in note for note in notes1))
        root = self.tmp / ".openbench"
        expected_files = (
            "openbench.toml",
            ".gitignore",
            "suites/default.toml",
            "profiles/local-codex.toml",
            "tasks/example-greeting/instruction.md",
            "tasks/example-greeting/task.toml",
            "tasks/example-greeting/environment/Dockerfile",
            "tasks/example-greeting/environment/app/README.md",
            "tasks/example-greeting/tests/test.sh",
            "tasks/example-greeting/solution/solve.sh",
        )
        for relative in expected_files:
            with self.subTest(path=relative):
                self.assertTrue((root / relative).is_file())
        for relative in ("jobs", "results", "trajectories"):
            self.assertTrue((root / relative).is_dir())

        suite = load_suite(root / "suites" / "default.toml")
        self.assertEqual(suite.harbor.version, init.HARBOR_VERSION)
        self.assertEqual(suite.harbor.commit, init.HARBOR_COMMIT)
        self.assertEqual(suite.task_sets[0].path, root / "tasks")
        self.assertEqual(suite.publication.scope, "local_only")
        profiles = load_profile_registry(self.tmp)
        self.assertEqual(profiles.get("local-codex").harness, "codex")

        task = tomllib.loads(
            (root / "tasks" / "example-greeting" / "task.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(task["schema_version"], "1.4")
        self.assertEqual(task["task"]["version"], "1.0.0")
        for relative in (
            "tasks/example-greeting/tests/test.sh",
            "tasks/example-greeting/solution/solve.sh",
        ):
            mode = (root / relative).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR)

        before = {
            relative: (root / relative).read_bytes() for relative in expected_files
        }
        notes2 = init.init_scaffold()
        self.assertTrue(all("skip (exists)" in note for note in notes2))
        after = {
            relative: (root / relative).read_bytes() for relative in expected_files
        }
        self.assertEqual(after, before)

    def test_gitignore_covers_all_private_runtime_directories(self):
        init.init_scaffold()
        lines = set(
            (self.tmp / ".openbench" / ".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertTrue({"jobs/", "results/", "trajectories/"} <= lines)

    def test_upgrade_isolates_legacy_tasks_and_augments_old_gitignore(self):
        root = self.tmp / ".openbench"
        legacy = root / "tasks" / "example"
        (legacy / "workspace").mkdir(parents=True)
        (legacy / "instruction.md").write_text(
            "# Old task\n", encoding="utf-8"
        )
        (legacy / "checker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        old_ignore = "# Old scaffold\nresults/\ntranscripts/\n"
        (root / ".gitignore").write_text(old_ignore, encoding="utf-8")

        notes = init.init_scaffold()

        self.assertEqual(
            (legacy / "instruction.md").read_text(encoding="utf-8"),
            "# Old task\n",
        )
        self.assertFalse((legacy / "task.toml").exists())
        self.assertTrue(
            (root / "harbor-tasks" / "example-greeting" / "task.toml").is_file()
        )
        suite = load_suite(root / "suites" / "default.toml")
        self.assertEqual(suite.task_sets[0].path, root / "harbor-tasks")
        ignore = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertTrue(ignore.startswith(old_ignore))
        self.assertIn("jobs/\n", ignore)
        self.assertIn("trajectories/\n", ignore)
        self.assertTrue(any("legacy/colliding" in note for note in notes))

        contents = (root / ".gitignore").read_bytes()
        init.init_scaffold()
        self.assertEqual((root / ".gitignore").read_bytes(), contents)

    def test_existing_example_collision_is_preserved_and_isolated(self):
        collision = (
            self.tmp / ".openbench" / "tasks" / "example-greeting"
        )
        collision.mkdir(parents=True)
        (collision / "user.txt").write_text("mine\n", encoding="utf-8")

        init.init_scaffold()

        self.assertEqual(
            sorted(path.name for path in collision.iterdir()),
            ["user.txt"],
        )
        suite = load_suite(
            self.tmp / ".openbench" / "suites" / "default.toml"
        )
        self.assertEqual(
            suite.task_sets[0].path,
            self.tmp / ".openbench" / "harbor-tasks",
        )

    def test_example_task_oracle_changes_reward_from_zero_to_one(self):
        init.init_scaffold()
        task = self.tmp / ".openbench" / "tasks" / "example-greeting"
        workspace = self.tmp / "workspace"
        shutil.copytree(task / "environment" / "app", workspace)
        reward = self.tmp / "reward"
        env = dict(os.environ, VERIFIER_LOGS_DIR=str(reward))

        def verify() -> float:
            subprocess.run(
                ["bash", str(task / "tests" / "test.sh")],
                cwd=workspace,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with (reward / "reward.json").open(encoding="utf-8") as handle:
                return json.load(handle)["reward"]

        self.assertEqual(verify(), 0.0)
        greeting = workspace / "greeting.txt"
        for near_miss in (b"hello", b"hello\n\n", b"hello\n\n\n"):
            with self.subTest(near_miss=near_miss):
                greeting.write_bytes(near_miss)
                self.assertEqual(verify(), 0.0)
        subprocess.run(
            ["bash", str(task / "solution" / "solve.sh")],
            cwd=workspace,
            check=True,
        )
        self.assertEqual(greeting.read_bytes(), b"hello\n")
        self.assertEqual(verify(), 1.0)

    def test_init_rejects_symlinked_openbench_root_without_escape(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.tmp / ".openbench").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(OSError, "symlink"):
            init.init_scaffold()

        self.assertEqual(list(outside.iterdir()), [])

    def test_init_rejects_symlinked_generated_directory_without_escape(self):
        root = self.tmp / ".openbench"
        outside = self.tmp / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "suites").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(OSError, "symlink"):
            init.init_scaffold()

        self.assertFalse((outside / "default.toml").exists())

    def test_init_rejects_symlinked_tasks_directory_without_escape(self):
        root = self.tmp / ".openbench"
        outside = self.tmp / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "tasks").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(OSError, "symlink"):
            init.init_scaffold()

        self.assertEqual(list(outside.iterdir()), [])

    def test_init_rejects_symlinked_generated_file_without_escape(self):
        root = self.tmp / ".openbench"
        outside = self.tmp / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "openbench.toml").symlink_to(outside / "escaped.toml")

        with self.assertRaisesRegex(OSError, "symlink"):
            init.init_scaffold()

        self.assertFalse((outside / "escaped.toml").exists())

    def test_init_rejects_nested_symlink_in_existing_example(self):
        init.init_scaffold()
        root = self.tmp / ".openbench"
        outside = self.tmp / "outside"
        outside.mkdir()
        test_script = root / "tasks" / "example-greeting" / "tests" / "test.sh"
        test_script.unlink()
        test_script.symlink_to(outside / "escaped.sh")

        with self.assertRaisesRegex(OSError, "symlink"):
            init.init_scaffold()

        self.assertFalse((outside / "escaped.sh").exists())

    def test_cli_prints_harbor_plan_command(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(init.main([]), 0)
        text = output.getvalue()
        self.assertIn("obench run --plan", text)
        self.assertNotIn("not wired", text)

    def test_legacy_task_is_separate_and_explicit(self):
        seed = self.tmp / "seed"
        seed.mkdir()
        (seed / "app.py").write_text("print('hi')\n", encoding="utf-8")

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                init.main(["--legacy-task", "demo", "--from", str(seed)]),
                0,
            )
        task = self.tmp / ".openbench" / "legacy-tasks" / "demo"
        self.assertTrue((task / "workspace" / "app.py").is_file())
        self.assertIn("LEGACY OpenBench task", output.getvalue())
        self.assertIn(".openbench/legacy-tasks", output.getvalue())
        self.assertFalse(
            (self.tmp / ".openbench" / "tasks" / "demo").exists()
        )
        load_suite(self.tmp / ".openbench" / "suites" / "default.toml")

    def test_deprecated_task_alias_remains_legacy(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(init.main(["--task", "alias-demo"]), 0)
        self.assertTrue(
            (
                self.tmp
                / ".openbench"
                / "legacy-tasks"
                / "alias-demo"
                / "instruction.md"
            ).is_file()
        )
        self.assertIn("LEGACY", output.getvalue())

    def test_cli_init_task_requires_from_pairing(self):
        with self.assertRaises(SystemExit) as context:
            init.main(["--from", "x"])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
