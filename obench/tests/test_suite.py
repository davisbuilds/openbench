"""Contract tests for strict Harbor-native suite authoring."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from obench.suite import SuiteError, load_suite


COMMIT = "72bc40b1e58b47a9cc6e0f14c29aced3a9e53767"


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="obench_suite_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        task = self.tmp / ".openbench" / "tasks" / "example"
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "instruction.md").write_text("# Example\n", encoding="utf-8")
        (task / "task.toml").write_text(
            'schema_version = "1.4"\n', encoding="utf-8"
        )
        self.suite_path = self.tmp / ".openbench" / "suites" / "default.toml"
        self.suite_path.parent.mkdir()

    def _valid(self) -> str:
        return f"""\
schema_version = 1
id = "private-default"
title = "Private repository benchmark"

[harbor]
version = "0.20.0"
commit = "{COMMIT}"

[[task_sets]]
id = "private"
kind = "local"
path = ".openbench/tasks"

[[task_sets]]
id = "external"
kind = "harbor"
name = "org/package"
ref = "sha256:{'a' * 64}"
git_commit = "{'b' * 40}"
subdir = "tasks/coding"

[[arms]]
id = "codex-sol"
harness = "codex"
profile = "local-codex"
model = "gpt-5.6-sol"

[run]
attempts = 3
concurrency = 2
max_retries = 1
timeout_seconds = 900

[evidence]
harbor_lock = true
verifier = true
trajectory = true
usage = true

[publication]
completeness = "complete"
"""

    def _load(self, contents: str | None = None):
        self.suite_path.write_text(contents or self._valid(), encoding="utf-8")
        return load_suite(self.suite_path)

    def test_loads_complete_suite_and_resolves_local_path(self):
        suite = self._load()
        self.assertEqual(suite.id, "private-default")
        self.assertEqual(suite.harbor.version, "0.20.0")
        self.assertEqual(suite.harbor.commit, COMMIT)
        self.assertEqual(suite.task_sets[0].path, self.tmp / ".openbench" / "tasks")
        self.assertEqual(suite.task_sets[1].name, "org/package")
        self.assertEqual(suite.arms[0].profile, "local-codex")
        self.assertEqual(suite.run.timeout_seconds, 900.0)
        self.assertTrue(suite.evidence.trajectory)
        self.assertEqual(suite.publication.completeness, "complete")

    def test_rejects_unknown_keys_at_every_level(self):
        cases = (
            ("title = ", 'credential_path = "/tmp/auth"\ntitle = '),
            ("commit = ", 'runtime_dir = "/tmp/jobs"\ncommit = '),
            ("path = ", 'tasks = ["example"]\npath = '),
            ("model = ", 'temperature = 0\nmodel = '),
            ("attempts = ", "seed = 1\nattempts = "),
            ("harbor_lock = ", "logs = true\nharbor_lock = "),
            ("completeness = ", 'partial_reason = "x"\ncompleteness = '),
        )
        for marker, replacement in cases:
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(SuiteError, "unknown keys"):
                    self._load(self._valid().replace(marker, replacement, 1))

    def test_rejects_duplicate_ids_and_duplicate_arms(self):
        duplicate_id = self._valid().replace(
            'id = "external"', 'id = "private"', 1
        )
        with self.assertRaisesRegex(SuiteError, "duplicate task set id"):
            self._load(duplicate_id)

        duplicate_arm = self._valid().replace(
            "\n[run]\n",
            """\

[[arms]]
id = "same-arm"
harness = "codex"
profile = "local-codex"
model = "gpt-5.6-sol"

[run]
""",
        )
        with self.assertRaisesRegex(SuiteError, "duplicate arm:"):
            self._load(duplicate_arm)

    def test_rejects_duplicate_external_source_with_mixed_hex_case(self):
        digest = "sha256:" + ("ab" * 32)
        duplicate = self._valid().replace(
            "\n[[arms]]\n",
            f"""\

[[task_sets]]
id = "same-external"
kind = "harbor"
name = "org/package"
ref = "{digest.upper().replace('SHA256:', 'sha256:')}"

[[arms]]
""",
        ).replace("sha256:" + ("a" * 64), digest, 1)
        with self.assertRaisesRegex(SuiteError, "duplicate task set source"):
            self._load(duplicate)

    def test_rejects_floating_external_refs_and_unpinned_git(self):
        with self.assertRaisesRegex(SuiteError, "immutable"):
            self._load(
                self._valid().replace(
                    f"sha256:{'a' * 64}", "latest", 1
                )
            )
        with self.assertRaisesRegex(SuiteError, "exact 40- or 64-hex"):
            self._load(
                self._valid().replace(f'"{"b" * 40}"', '"main"', 1)
            )
        with self.assertRaisesRegex(SuiteError, "subdir requires git_commit"):
            self._load(
                self._valid().replace(f'git_commit = "{"b" * 40}"\n', "", 1)
            )

    def test_rejects_path_escape_absolute_path_and_symlinks(self):
        for unsafe in ("../outside", "/tmp/tasks", "~/tasks"):
            with self.subTest(path=unsafe):
                with self.assertRaisesRegex(SuiteError, "safe relative"):
                    self._load(
                        self._valid().replace(
                            'path = ".openbench/tasks"',
                            f'path = "{unsafe}"',
                            1,
                        )
                    )

        link = self.tmp / ".openbench" / "linked-tasks"
        os.symlink(self.tmp / ".openbench" / "tasks", link)
        with self.assertRaisesRegex(SuiteError, "symlinks"):
            self._load(
                self._valid().replace(
                    'path = ".openbench/tasks"',
                    'path = ".openbench/linked-tasks"',
                    1,
                )
            )

        link.unlink()
        os.symlink(
            self.tmp / ".openbench" / "tasks" / "example" / "instruction.md",
            self.tmp / ".openbench" / "tasks" / "example" / "linked.md",
        )
        with self.assertRaisesRegex(SuiteError, "contains a symlink"):
            self._load()

        (
            self.tmp / ".openbench" / "tasks" / "example" / "linked.md"
        ).unlink()
        staging = self.tmp / ".openbench" / "tasks" / "staging"
        staging.mkdir()
        os.symlink(self.tmp / "outside", staging / "nested-link")
        with self.assertRaisesRegex(SuiteError, "contains a symlink"):
            self._load()

    def test_rejects_unsafe_profile_and_runtime_numbers(self):
        with self.assertRaisesRegex(SuiteError, "profile"):
            self._load(
                self._valid().replace(
                    'profile = "local-codex"',
                    'profile = "/tmp/auth.json"',
                    1,
                )
            )
        for field, value in (
            ("attempts", "true"),
            ("concurrency", "0"),
            ("max_retries", "-1"),
            ("timeout_seconds", "nan"),
            ("timeout_seconds", "inf"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(SuiteError):
                    self._load(
                        self._valid().replace(
                            f"{field} = {3 if field == 'attempts' else 2 if field == 'concurrency' else 1 if field == 'max_retries' else 900}",
                            f"{field} = {value}",
                            1,
                        )
                    )

    def test_rejects_partial_or_empty_local_task_sets(self):
        task = self.tmp / ".openbench" / "tasks" / "example"
        (task / "instruction.md").unlink()
        with self.assertRaisesRegex(SuiteError, "partial Harbor tasks"):
            self._load()
        (task / "task.toml").unlink()
        with self.assertRaisesRegex(SuiteError, "contains no Harbor tasks"):
            self._load()

    def test_rejects_suite_file_symlink(self):
        real = self.tmp / "real.toml"
        real.write_text(self._valid(), encoding="utf-8")
        self.suite_path.symlink_to(real)
        with self.assertRaisesRegex(SuiteError, "must not be a symlink"):
            load_suite(self.suite_path)


if __name__ == "__main__":
    unittest.main()
