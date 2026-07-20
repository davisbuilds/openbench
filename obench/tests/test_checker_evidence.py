#!/usr/bin/env python3
"""Tests for checker evidence captured into result rows."""

import json
import os

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys
import tempfile
import time
import unittest
from unittest import mock
from obench import run  # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class CheckerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_checker_evidence_")
        self.tasks_dir = os.path.join(self.tmp, "tasks")
        self.task = "chatty-checker"
        self.task_dir = os.path.join(self.tasks_dir, self.task)
        os.makedirs(os.path.join(self.task_dir, "workspace"))
        _write(os.path.join(self.task_dir, "instruction.md"), "do it\n")
        _write(os.path.join(self.task_dir, "workspace", "root.txt"), "root evidence\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_checker(self, script):
        path = os.path.join(self.task_dir, "checker.sh")
        _write(path, script)
        os.chmod(path, 0o755)

    def _row(self):
        return run.run_cell(
            "null", self.task, "gpt-5.5-medium", 1, 60,
            self.tasks_dir, os.path.join(BENCH_DIR, "tests", "fixtures"), 30,
        )

    def test_checker_stdout_stderr_and_workspace_evidence_land_on_row(self):
        self._set_checker("""#!/usr/bin/env bash
set -euo pipefail
echo 'hello stdout'
echo 'hello stderr' >&2
exit 1
""")
        row = self._row()

        self.assertFalse(row["success"])
        self.assertEqual(row["checker_exit"], 1)
        self.assertEqual(row["checker_stdout"], "hello stdout\n")
        self.assertEqual(row["checker_stderr"], "hello stderr\n")
        evidence = row["checker_workspace_files"]
        self.assertIn("root.txt", evidence)
        self.assertEqual(evidence["root.txt"]["bytes"], len("root evidence\n"))
        self.assertEqual(
            evidence["root.txt"]["sha256"],
            "b142a1dc0018dcffc9e87c04ef649c61bdbc8628822fad6882c8d328715b2633",
        )
        self.assertIsInstance(evidence["root.txt"].get("mtime"), (int, float))

    def test_checker_output_is_truncated_to_last_8000_chars_with_marker(self):
        stdout = ("x" * 25) + "\n" + ("!" * (run.CHECKER_CAPTURE_LIMIT - 1))
        stderr = ("x" * 10) + "\n" + ("?" * (run.CHECKER_CAPTURE_LIMIT - 1))
        self._set_checker("""#!/usr/bin/env bash
python3 - <<'PY'
import sys
sys.stdout.write(%r)
sys.stderr.write(%r)
sys.exit(1)
PY
""" % (stdout, stderr))
        row = self._row()

        self.assertTrue(row["checker_stdout"].startswith(run.CHECKER_CAPTURE_TRUNCATED_PREFIX))
        self.assertEqual(row["checker_stdout"], run.CHECKER_CAPTURE_TRUNCATED_PREFIX + stdout[-run.CHECKER_CAPTURE_LIMIT:])
        self.assertTrue(row["checker_stderr"].startswith(run.CHECKER_CAPTURE_TRUNCATED_PREFIX))
        self.assertEqual(row["checker_stderr"], run.CHECKER_CAPTURE_TRUNCATED_PREFIX + stderr[-run.CHECKER_CAPTURE_LIMIT:])

    def test_large_workspace_files_are_not_hashed(self):
        big_path = os.path.join(self.task_dir, "workspace", "big.bin")
        with open(big_path, "wb") as fh:
            fh.truncate(run.WORKSPACE_EVIDENCE_MAX_BYTES + 1)
        self._set_checker("""#!/usr/bin/env bash
exit 1
""")
        row = self._row()

        item = row["checker_workspace_files"]["big.bin"]
        self.assertEqual(item["bytes"], run.WORKSPACE_EVIDENCE_MAX_BYTES + 1)
        self.assertIn("too_large", item["skipped"])
        self.assertNotIn("sha256", item)

    def test_workspace_evidence_skips_symlinks_and_honors_aggregate_limits(self):
        workspace = os.path.join(self.task_dir, "workspace")
        os.makedirs(os.path.join(workspace, "real-dir"))
        _write(os.path.join(workspace, "real-dir", "nested.txt"), "nested evidence")
        os.symlink("root.txt", os.path.join(workspace, "root-link.txt"))
        os.symlink("real-dir", os.path.join(workspace, "dir-link"))
        _write(os.path.join(workspace, "extra.txt"), "extra")

        all_evidence = run.capture_workspace_files(workspace)
        self.assertEqual(all_evidence["root-link.txt"]["skipped"], "not_regular")
        self.assertEqual(all_evidence["dir-link"]["skipped"], "not_regular")
        self.assertIn("sha256", all_evidence["real-dir/nested.txt"])

        limited = run.capture_workspace_files(workspace, max_total_bytes=1, max_files=2)
        self.assertEqual(limited["dir-link"]["skipped"], "not_regular")
        self.assertEqual(limited["extra.txt"]["skipped"], "total_bytes_limit>1")
        self.assertIn(run.WORKSPACE_EVIDENCE_META_KEY, limited)
        self.assertEqual(limited[run.WORKSPACE_EVIDENCE_META_KEY]["skipped"], "entry_count_limit>2")

    def test_checker_background_process_cannot_hold_capture_open(self):
        self._set_checker("""#!/usr/bin/env bash
(sleep 30) &
echo done
exit 1
""")
        start = time.monotonic()
        row = self._row()

        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(row["checker_stdout"], "done\n")
        self.assertEqual(row["checker_exit"], 1)

    @unittest.skipUnless(hasattr(os, "setsid"), "requires POSIX sessions")
    def test_checker_escaped_background_writer_cannot_hold_capture_open(self):
        self._set_checker("""#!/usr/bin/env bash
python3 - <<'PY' &
import os, sys, time
os.setsid()
with open(os.path.join(os.environ['TASK_DIR'], 'escaped.pid'), 'w') as fh:
    fh.write(str(os.getpid()))
while True:
    print('escaped child still writing', flush=True)
    time.sleep(0.1)
PY
echo done
exit 1
""")
        start = time.monotonic()
        row = self._row()

        self.assertLess(time.monotonic() - start, 8)
        self.assertIn("done\n", row["checker_stdout"])
        self.assertEqual(row["checker_exit"], 1)
        pid_path = os.path.join(self.task_dir, "escaped.pid")
        with open(pid_path, encoding="utf-8") as fh:
            escaped_pid = int(fh.read())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(escaped_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"escaped checker descendant still running: {escaped_pid}")

    def test_streaming_env_redactor_redacts_across_chunks_before_tailing(self):
        secret = "private-start\nprivate-middle\nprivate-end"
        redactor = run.EnvValueRedactor({"MULTILINE_SECRET": secret})
        capture = run.TailCapture(limit=32)
        first = "a" * 4086 + secret[:10]
        second = secret[10:] + ("!" * 8000)
        capture.append(redactor.feed(first))
        capture.append(redactor.feed(second))
        capture.append(redactor.close())
        text = run.scrub_checker_output(capture.text())

        self.assertTrue(text.startswith(run.CHECKER_CAPTURE_TRUNCATED_PREFIX))
        self.assertNotIn("private-middle", text)
        self.assertNotIn("private-end", text)

    def test_truncation_boundary_redacts_after_leading_whitespace(self):
        text = run.scrub_checker_output(
            run.CHECKER_CAPTURE_TRUNCATED_PREFIX + "   partial-secret-fragment rest")
        self.assertEqual(
            text,
            run.CHECKER_CAPTURE_TRUNCATED_PREFIX + "   <REDACTED_BOUNDARY> rest",
        )

    def test_workspace_evidence_paths_are_scrubbed(self):
        secret = "secretfilename1234"
        _write(os.path.join(self.task_dir, "workspace", f"{secret}.txt"), "x")
        self._set_checker("""#!/usr/bin/env bash
exit 1
""")
        old = os.environ.get("FILENAME_SECRET")
        os.environ["FILENAME_SECRET"] = secret
        try:
            row = self._row()
        finally:
            if old is None:
                os.environ.pop("FILENAME_SECRET", None)
            else:
                os.environ["FILENAME_SECRET"] = old

        encoded_keys = json.dumps(sorted(row["checker_workspace_files"].keys()))
        self.assertNotIn(secret, encoded_keys)
        self.assertIn("<REDACTED_ENV>.txt", row["checker_workspace_files"])

    def test_checker_output_is_scrubbed_before_persisting(self):
        self._set_checker("""#!/usr/bin/env bash
echo 'secret sk-abcDEF0123456789ghijklmnop'
echo "$DATABASE_URL"
echo "$MULTILINE_SECRET"
python3 - <<'PY'
print('!' * 9000)
PY
exit 1
""")
        old_db = os.environ.get("DATABASE_URL")
        old_multi = os.environ.get("MULTILINE_SECRET")
        os.environ["DATABASE_URL"] = "postgres://bench-user:secretpass@example.invalid/db"
        os.environ["MULTILINE_SECRET"] = "private-start\nprivate-middle\nprivate-end"
        try:
            row = self._row()
        finally:
            if old_db is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_db
            if old_multi is None:
                os.environ.pop("MULTILINE_SECRET", None)
            else:
                os.environ["MULTILINE_SECRET"] = old_multi

        self.assertTrue(row["checker_stdout"].startswith(run.CHECKER_CAPTURE_TRUNCATED_PREFIX))
        self.assertNotIn("sk-abcDEF0123456789ghijklmnop", row["checker_stdout"])
        self.assertNotIn("secretpass", row["checker_stdout"])
        self.assertNotIn("private-middle", row["checker_stdout"])

    def test_append_row_persists_additive_fields(self):
        self._set_checker("""#!/usr/bin/env bash
echo out
echo err >&2
exit 1
""")
        row = self._row()
        results = os.path.join(self.tmp, "results.jsonl")
        run.append_row(results, row)

        with open(results, encoding="utf-8") as fh:
            saved = json.loads(fh.readline())
        self.assertEqual(saved["checker_stdout"], "out\n")
        self.assertEqual(saved["checker_stderr"], "err\n")
        self.assertIn("root.txt", saved["checker_workspace_files"])
        self.assertIn("image_digest", saved)

    def test_docker_image_digest_prefers_repo_digest_then_image_id(self):
        def fake_run(cmd, **kwargs):
            class Proc:
                returncode = 0
                stderr = ""
                stdout = "repo@sha256:abc\n" if "RepoDigests" in cmd[-2] else "sha256:id\n"
            return Proc()

        with mock.patch.object(run.subprocess, "run", side_effect=fake_run):
            self.assertEqual(run.docker_image_digest("img:tag"), "repo@sha256:abc")

        calls = []
        def fake_fallback(cmd, **kwargs):
            calls.append(cmd)
            class Proc:
                stderr = ""
                if "RepoDigests" in cmd[-2]:
                    returncode = 1
                    stdout = ""
                else:
                    returncode = 0
                    stdout = "sha256:id\n"
            return Proc()

        with mock.patch.object(run.subprocess, "run", side_effect=fake_fallback):
            self.assertEqual(run.docker_image_digest("img:tag"), "sha256:id")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
