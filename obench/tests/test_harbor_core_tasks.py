#!/usr/bin/env python3
"""Contract tests for the canonical OpenBench Lite Harbor task set."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import tomllib
import unittest

from obench import export_harbor, harbor_run, workspace
from obench.paths import SOURCE_ROOT
from obench.publish import DIGEST_SCHEME_CURRENT, task_content_digest


CORE_TASKS = {
    "add-feature",
    "build-a-cli",
    "fix-failing-test",
    "make-ci-green",
    "make-it-run",
    "misleading-error",
    "taskflow",
    "webcore",
}
SOURCE_COMMIT = "802014700f6b3c62eddc1a406e3062a438ce572f"
LEGACY_ROOT = Path(SOURCE_ROOT) / "tasks"
HARBOR_ROOT = Path(SOURCE_ROOT) / "harbor-tasks" / "openbench-lite"


def _tree(root: Path, *, exclude: set[str] | None = None) -> dict[str, tuple[bytes, int]]:
    if not root.is_dir():
        return {}
    excluded = exclude or set()
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if relative.as_posix() in excluded:
            continue
        files[relative.as_posix()] = (
            path.read_bytes(),
            path.stat().st_mode & 0o111,
        )
    return files


def _pinned_task_content_digest(name: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        workspace._git_archive_extract(
            str(SOURCE_ROOT),
            SOURCE_COMMIT,
            tmp,
            f"tasks/{name}",
        )
        return task_content_digest(
            tmp,
            scheme=DIGEST_SCHEME_CURRENT,
        )


class HarborCoreTaskContractTests(unittest.TestCase):
    def test_tree_ignores_only_generated_python_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "authored.py").write_text("VALUE = 1\n")
            (root / "authored.txt").write_text("keep\n")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "authored.cpython-314.pyc").write_bytes(
                b"generated"
            )
            (root / "loose.pyc").write_bytes(b"generated")
            (root / "loose.pyo").write_bytes(b"generated")

            self.assertEqual(
                set(_tree(root)),
                {"authored.py", "authored.txt"},
            )

    def test_task_set_is_complete_and_harbor_native(self):
        actual = {
            path.name
            for path in HARBOR_ROOT.iterdir()
            if path.is_dir()
        }
        self.assertEqual(actual, CORE_TASKS)

        for name in sorted(CORE_TASKS):
            with self.subTest(task=name):
                task = HARBOR_ROOT / name
                required = {
                    "instruction.md",
                    "task.toml",
                    "environment/Dockerfile",
                    "tests/test.sh",
                    "solution/solve.sh",
                }
                self.assertTrue(
                    required.issubset(_tree(task)),
                    f"{name} is missing required Harbor task files",
                )
                self.assertFalse((task / "checker.sh").exists())
                self.assertFalse((task / "workspace").exists())
                self.assertTrue(os.access(task / "tests" / "test.sh", os.X_OK))
                self.assertTrue(os.access(task / "solution" / "solve.sh", os.X_OK))

    def test_task_toml_uses_pinned_schema_source_and_execution_contract(self):
        allowed_root = {
            "schema_version",
            "source",
            "artifacts",
            "task",
            "metadata",
            "verifier",
            "agent",
            "environment",
        }
        allowed_task = {"name", "description", "authors", "keywords"}
        allowed_environment = {
            "build_timeout_sec",
            "network_mode",
            "os",
            "cpus",
            "gpus",
        }

        for name in sorted(CORE_TASKS):
            with self.subTest(task=name):
                task = HARBOR_ROOT / name
                config = tomllib.loads((task / "task.toml").read_text())
                self.assertEqual(set(config), allowed_root)
                self.assertEqual(config["schema_version"], "1.4")
                self.assertEqual(
                    config["source"],
                    f"tasks/{name}@{SOURCE_COMMIT}",
                )
                self.assertEqual(
                    config["artifacts"],
                    [{"source": "/app", "destination": "workspace"}],
                )
                self.assertEqual(set(config["task"]), allowed_task)
                self.assertEqual(config["task"]["name"], f"openbench/{name}")
                self.assertEqual(
                    config["task"]["authors"],
                    [{"name": "Matthew Lam"}],
                )
                self.assertEqual(config["metadata"]["origin"], "openbench")
                self.assertEqual(
                    config["metadata"]["source_task"],
                    f"tasks/{name}",
                )
                self.assertEqual(
                    config["metadata"]["source_commit"],
                    SOURCE_COMMIT,
                )
                self.assertNotIn("difficulty", config["metadata"])
                self.assertEqual(config["agent"], {"timeout_sec": 2400.0})
                self.assertEqual(config["verifier"], {"timeout_sec": 120.0})
                self.assertEqual(
                    set(config["environment"]),
                    allowed_environment,
                )
                self.assertEqual(
                    config["environment"],
                    {
                        "build_timeout_sec": 600.0,
                        "network_mode": "public",
                        "os": "linux",
                        "cpus": 4,
                        "gpus": 0,
                    },
                )

    def test_every_task_is_accepted_by_harbor_oauth_runner(self):
        for name in sorted(CORE_TASKS):
            with self.subTest(task=name):
                task = (HARBOR_ROOT / name).resolve()
                self.assertEqual(harbor_run.validate_task_root(task), task)

    def test_verifier_evidence_records_public_networking(self):
        for name in sorted(CORE_TASKS):
            with self.subTest(task=name):
                test_sh = (
                    HARBOR_ROOT / name / "tests" / "test.sh"
                ).read_text()
                self.assertIn('"network_mode": "public"', test_sh)
                self.assertNotIn('"network_mode": "no-network"', test_sh)

    def test_native_payloads_match_all_legacy_sources(self):
        for name in sorted(CORE_TASKS):
            with self.subTest(task=name):
                legacy = LEGACY_ROOT / name
                native = HARBOR_ROOT / name
                config = tomllib.loads((native / "task.toml").read_text())

                self.assertEqual(
                    (native / "instruction.md").read_bytes(),
                    (legacy / "instruction.md").read_bytes(),
                )
                self.assertEqual(
                    _tree(native / "environment" / "app"),
                    _tree(legacy / "workspace"),
                )
                self.assertEqual(
                    (native / "tests" / "checker.sh").read_bytes(),
                    (legacy / "checker.sh").read_bytes(),
                )
                self.assertEqual(
                    _tree(native / "tests" / "checker_data"),
                    _tree(legacy / "checker_data"),
                )
                self.assertEqual(
                    _tree(native / "solution", exclude={"solve.sh"}),
                    _tree(legacy / "solution", exclude={"solve.sh"}),
                )
                digest = config["metadata"]["openbench_task_content_digest"]
                self.assertEqual(digest["scheme"], DIGEST_SCHEME_CURRENT)
                self.assertEqual(
                    digest["sha256"],
                    _pinned_task_content_digest(name),
                )

    def test_every_untouched_task_fails_and_oracle_passes(self):
        for name in sorted(CORE_TASKS):
            with self.subTest(task=name):
                untouched, solved = export_harbor.round_trip_polarity(
                    str(HARBOR_ROOT / name)
                )
                self.assertLess(
                    untouched,
                    1.0,
                    f"{name} unexpectedly passes without agent changes",
                )
                self.assertEqual(
                    solved,
                    1.0,
                    f"{name} oracle does not earn full reward",
                )


if __name__ == "__main__":
    unittest.main()
