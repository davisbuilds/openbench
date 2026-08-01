#!/usr/bin/env python3
"""Tests for OpenBench → Harbor export bridge."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from obench import export_harbor as eh
from obench.paths import SOURCE_ROOT


CORE_TASKS = (
    "add-feature",
    "build-a-cli",
    "fix-failing-test",
    "make-ci-green",
    "make-it-run",
    "misleading-error",
    "taskflow",
    "webcore",
)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _git(cwd: str, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


class MapScoreTests(unittest.TestCase):
    def test_exit_zero_is_full_reward(self):
        self.assertEqual(eh.map_checker_to_reward(0, "ok\nSCORE: 0.2\n"), 1.0)

    def test_nonzero_without_score_is_zero(self):
        self.assertEqual(eh.map_checker_to_reward(1, "FAIL\n"), 0.0)

    def test_nonzero_with_score_uses_last_score(self):
        out = "SCORE: 0.1\npartial\nSCORE: 0.75\n"
        self.assertEqual(eh.map_checker_to_reward(2, out), 0.75)


class TaskTomlTests(unittest.TestCase):
    def test_schema_and_metadata(self):
        text = eh.render_task_toml(
            task_name="make-it-run",
            description="Get the script running.",
            workspace_provenance=None,
        )
        self.assertIn('schema_version = "1.3"', text)
        self.assertIn('name = "openbench/make-it-run"', text)
        self.assertIn('origin = "openbench"', text)
        self.assertIn('difficulty = "unknown"', text)
        self.assertIn('openbench_workspace_kind = "snapshot"', text)
        self.assertIn('network_mode = "no-network"', text)
        self.assertIn("[environment]", text)
        self.assertIn("[verifier]", text)

    def test_git_provenance_fields(self):
        text = eh.render_task_toml(
            task_name="billing",
            description="Fix billing.",
            workspace_provenance={
                "kind": "git",
                "repo": ".",
                "ref": "abc",
                "resolved_sha": "a" * 40,
                "subdir": "services/billing",
            },
        )
        self.assertIn('openbench_workspace_kind = "git"', text)
        self.assertIn('openbench_workspace_resolved_sha = "' + ("a" * 40) + '"', text)
        self.assertIn('openbench_workspace_subdir = "services/billing"', text)


class TestShScoreMappingTests(unittest.TestCase):
    def test_generated_test_sh_maps_score_line(self):
        tmp = tempfile.mkdtemp(prefix="obench_harbor_score_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tests = os.path.join(tmp, "tests")
        os.makedirs(tests)
        _write(
            os.path.join(tests, "checker.sh"),
            "#!/usr/bin/env bash\necho 'SCORE: 0.4'\nexit 7\n",
        )
        os.chmod(os.path.join(tests, "checker.sh"), 0o755)
        _write(os.path.join(tests, "test.sh"), eh.render_test_sh())
        os.chmod(os.path.join(tests, "test.sh"), 0o755)
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        reward_dir = os.path.join(tmp, "reward")
        env = dict(os.environ)
        env["VERIFIER_LOGS_DIR"] = reward_dir
        proc = subprocess.run(
            ["bash", os.path.join(tests, "test.sh")],
            cwd=work,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(eh.read_reward_file(reward_dir), 0.4)

    def test_exit_zero_writes_one(self):
        tmp = tempfile.mkdtemp(prefix="obench_harbor_ok_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tests = os.path.join(tmp, "tests")
        os.makedirs(tests)
        _write(os.path.join(tests, "checker.sh"), "#!/usr/bin/env bash\nexit 0\n")
        os.chmod(os.path.join(tests, "checker.sh"), 0o755)
        _write(os.path.join(tests, "test.sh"), eh.render_test_sh())
        os.chmod(os.path.join(tests, "test.sh"), 0o755)
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        reward_dir = os.path.join(tmp, "reward")
        env = dict(os.environ)
        env["VERIFIER_LOGS_DIR"] = reward_dir
        proc = subprocess.run(
            ["bash", os.path.join(tests, "test.sh")],
            cwd=work,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(eh.read_reward_file(reward_dir), 1.0)


class SolutionOverlayTests(unittest.TestCase):
    def test_solve_sh_overlays_files(self):
        tmp = tempfile.mkdtemp(prefix="obench_harbor_sol_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        solution = os.path.join(tmp, "solution")
        os.makedirs(os.path.join(solution, "pkg"))
        _write(os.path.join(solution, "solve.sh"), eh.render_solve_sh())
        os.chmod(os.path.join(solution, "solve.sh"), 0o755)
        _write(os.path.join(solution, "pkg", "app.py"), "print('fixed')\n")
        _write(os.path.join(solution, "root.txt"), "root\n")
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        _write(os.path.join(work, "pkg", "app.py"), "print('broken')\n")
        proc = subprocess.run(
            ["bash", os.path.join(solution, "solve.sh")],
            cwd=work,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        with open(os.path.join(work, "pkg", "app.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "print('fixed')\n")
        self.assertTrue(os.path.isfile(os.path.join(work, "root.txt")))
        self.assertFalse(os.path.isfile(os.path.join(work, "solve.sh")))

    def test_solve_sh_overlays_shell_deliverables(self):
        tmp = tempfile.mkdtemp(prefix="obench_harbor_shell_sol_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        solution = os.path.join(tmp, "solution")
        os.makedirs(solution)
        _write(os.path.join(solution, "solve.sh"), eh.render_solve_sh())
        _write(os.path.join(solution, "tool.sh"), "#!/bin/sh\necho fixed\n")
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        proc = subprocess.run(
            ["bash", os.path.join(solution, "solve.sh")],
            cwd=work,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(os.path.isfile(os.path.join(work, "tool.sh")))


class ExportTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_harbor_export_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task = os.path.join(self.tmp, "tasks", "demo")
        os.makedirs(os.path.join(self.task, "workspace"))
        os.makedirs(os.path.join(self.task, "solution"))
        os.makedirs(os.path.join(self.task, "checker_data"))
        _write(os.path.join(self.task, "instruction.md"), "Fix demo.\n\nDetails.\n")
        _write(
            os.path.join(self.task, "checker.sh"),
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'test -f "$TASK_DIR/checker_data/flag.txt"\n'
            'got="$(cat greeting.txt 2>/dev/null || true)"\n'
            'if [ "$got" = "hi" ]; then exit 0; fi\n'
            "echo SCORE: 0.0\n"
            "exit 1\n",
        )
        os.chmod(os.path.join(self.task, "checker.sh"), 0o755)
        _write(os.path.join(self.task, "checker_data", "flag.txt"), "1\n")
        _write(os.path.join(self.task, "workspace", "greeting.txt"), "nope\n")
        _write(os.path.join(self.task, "solution", "greeting.txt"), "hi\n")

    def test_export_layout(self):
        out = os.path.join(self.tmp, "harbor", "demo")
        summary = eh.export_task(self.task, out)
        self.assertEqual(summary["workspace_mode"], "snapshot")
        self.assertTrue(summary["has_solution"])
        self.assertTrue(os.path.isfile(os.path.join(out, "instruction.md")))
        self.assertTrue(os.path.isfile(os.path.join(out, "task.toml")))
        self.assertTrue(os.path.isfile(os.path.join(out, "environment", "Dockerfile")))
        self.assertTrue(
            os.path.isfile(os.path.join(out, "environment", "app", "greeting.txt"))
        )
        self.assertTrue(os.path.isfile(os.path.join(out, "tests", "test.sh")))
        self.assertTrue(os.path.isfile(os.path.join(out, "tests", "checker.sh")))
        self.assertTrue(
            os.path.isfile(os.path.join(out, "tests", "checker_data", "flag.txt"))
        )
        self.assertTrue(os.path.isfile(os.path.join(out, "solution", "solve.sh")))
        self.assertTrue(
            os.path.isfile(os.path.join(out, "solution", "greeting.txt"))
        )
        with open(os.path.join(out, "environment", "Dockerfile"), encoding="utf-8") as fh:
            docker = fh.read()
        self.assertIn("FROM python:3.11-slim", docker)
        self.assertIn("COPY app/ /app/", docker)
        # No results/transcripts/auth leakage paths.
        for banned in ("results", "transcripts", "auth"):
            self.assertFalse(os.path.exists(os.path.join(out, banned)))

    def test_round_trip_polarity_on_fixture(self):
        out = os.path.join(self.tmp, "harbor", "demo")
        eh.export_task(self.task, out)
        r0, r1 = eh.round_trip_polarity(out)
        self.assertEqual(r0, 0.0)
        self.assertEqual(r1, 1.0)

    def test_export_preserves_procedural_solution(self):
        procedural = "#!/usr/bin/env bash\nprintf 'hi\\n' > greeting.txt\n"
        solve = os.path.join(self.task, "solution", "solve.sh")
        _write(solve, procedural)
        os.chmod(solve, 0o700)
        os.unlink(os.path.join(self.task, "solution", "greeting.txt"))

        out = os.path.join(self.tmp, "harbor", "procedural")
        eh.export_task(self.task, out)
        exported = os.path.join(out, "solution", "solve.sh")
        with open(exported, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), procedural)
        self.assertTrue(os.stat(exported).st_mode & 0o100)
        r0, r1 = eh.round_trip_polarity(out)
        self.assertEqual((r0, r1), (0.0, 1.0))

    def test_git_mode_records_sha(self):
        root = os.path.join(self.tmp, "gitroot")
        os.makedirs(root)
        _git(root, "init")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "T")
        _git(root, "checkout", "-b", "main")
        _write(os.path.join(root, "svc", "app.py"), "x = 1\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "init")
        sha = _git(root, "rev-parse", "HEAD")

        task = os.path.join(root, "tasks", "gitdemo")
        os.makedirs(task)
        _write(os.path.join(task, "instruction.md"), "Git demo.\n")
        _write(
            os.path.join(task, "checker.sh"),
            "#!/usr/bin/env bash\ntest -f app.py && exit 0; exit 1\n",
        )
        os.chmod(os.path.join(task, "checker.sh"), 0o755)
        _write(
            os.path.join(task, "workspace.toml"),
            f'kind = "git"\nrepo = "."\nref = "{sha}"\nsubdir = "svc"\n',
        )
        os.makedirs(os.path.join(task, "solution"))
        _write(os.path.join(task, "solution", "app.py"), "x = 1\n")

        out = os.path.join(self.tmp, "harbor_git", "gitdemo")
        summary = eh.export_task(task, out)
        self.assertEqual(summary["workspace_mode"], "git")
        self.assertEqual(summary["workspace_provenance"]["resolved_sha"], sha)
        self.assertTrue(
            os.path.isfile(os.path.join(out, "environment", "app", "app.py"))
        )
        with open(os.path.join(out, "task.toml"), encoding="utf-8") as fh:
            toml = fh.read()
        self.assertIn(f'openbench_workspace_resolved_sha = "{sha}"', toml)


@unittest.skipUnless(
    os.path.isdir(os.path.join(SOURCE_ROOT, "tasks", "make-it-run")),
    "core tasks/ not present in this install layout",
)
class CoreTasksHarborRoundTripTests(unittest.TestCase):
    """Export all 8 core tasks and assert Harbor-bridge polarity."""

    @classmethod
    def setUpClass(cls):
        cls.tasks_dir = os.path.join(SOURCE_ROOT, "tasks")
        cls.out_root = tempfile.mkdtemp(prefix="obench_harbor_core_")
        cls.summaries = eh.export_tasks(cls.tasks_dir, cls.out_root, "all")
        cls.by_name = {s["task_name"]: s for s in cls.summaries}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_root, ignore_errors=True)

    def test_exports_all_eight_core_tasks(self):
        self.assertEqual(sorted(self.by_name), sorted(CORE_TASKS))

    def test_polarity_holds_for_each_core_task(self):
        """Harbor bridge preserves OpenBench polarity through reward mapping.

        Untouched workspaces must not be fully solved (reward < 1.0). Partial-
        credit checkers may emit a baseline SCORE > 0; the bridge must surface
        that float. Oracle overlay must yield reward 1.0.
        """
        failures = []
        for name in CORE_TASKS:
            exported = self.by_name[name]["out_dir"]
            r0, r1 = eh.round_trip_polarity(exported)
            if not (r0 < 1.0) or r1 != 1.0:
                failures.append(f"{name}: untouched={r0!r} oracle={r1!r}")
        self.assertEqual(failures, [], "\n".join(failures))

        # Binary (non-partial) core tasks stay at reward 0.0 when untouched.
        for name in ("build-a-cli", "fix-failing-test", "make-it-run", "misleading-error"):
            r0, _r1 = eh.round_trip_polarity(self.by_name[name]["out_dir"])
            self.assertEqual(r0, 0.0, name)

    def test_cli_export_harbor_smoke(self):
        out = tempfile.mkdtemp(prefix="obench_harbor_cli_")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        rc = eh.main([
            "harbor",
            "--task", "make-it-run",
            "--tasks-dir", self.tasks_dir,
            "--out", out,
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(out, "make-it-run", "task.toml")))
        with open(os.path.join(out, "make-it-run", "task.toml"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertRegex(body, re.compile(r'schema_version\s*=\s*"1\.3"'))


if __name__ == "__main__":
    unittest.main()
