#!/usr/bin/env python3
"""Tests for obench publish / verify show-off bundles."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from obench import publish
from obench import scrub

# The e2e tests run `python -m obench.cli` from a temp cwd; the repo root must
# be importable regardless of where the suite is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SUBPROC_ENV = {**os.environ, "PYTHONPATH": _REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")}


def _row(harness, task, trial, success, *, candidate=None, model="model-x", **extra):
    row = {
        "run_id": f"{harness}:{task}:{model}:trial{trial}",
        "harness": harness,
        "model": model,
        "task": task,
        "trial": trial,
        "success": success,
        "score": 1.0 if success else 0.0,
        "failure_class": "solved" if success else "wrong_answer",
        "wall_time_s": 10.0 + trial,
        "tokens_input_uncached": 100,
        "tokens_output": 20,
        "tokens_cache_read": 50,
        "token_basis": "vendor_split",
        "harness_version": "1.0",
        "timeout_s": 60,
        "completed": True,
        "candidate_provenance": None,
    }
    if candidate is not None:
        row["harness"] = candidate
        row["candidate_provenance"] = {
            "name": candidate,
            "candidate_digest": hashlib.sha256(
                f"{candidate}-spec".encode()
            ).hexdigest(),
            "kind": "manifest",
        }
        row["run_id"] = f"{candidate}:{task}:{model}:trial{trial}"
        row["token_basis"] = extra.pop("token_basis", "unmetered")
    row.update(extra)
    return row


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _make_task(root, name):
    task_dir = os.path.join(root, name)
    os.makedirs(os.path.join(task_dir, "workspace"), exist_ok=True)
    with open(os.path.join(task_dir, "instruction.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\nDo the thing.\n")
    with open(os.path.join(task_dir, "checker.sh"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    with open(os.path.join(task_dir, "workspace", "main.py"), "w", encoding="utf-8") as fh:
        fh.write("print('hi')\n")
    return task_dir


class PublishBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.makedirs(self.tasks)
        _make_task(self.tasks, "alpha")
        _make_task(self.tasks, "beta")
        self.results = os.path.join(self.tmp.name, "results.jsonl")
        self.out = os.path.join(self.tmp.name, "bundle")
        self.gate_dir = os.path.join(self.tmp.name, "gate")
        os.makedirs(self.gate_dir)
        with open(os.path.join(self.gate_dir, "mycli-gate.json"), "w", encoding="utf-8") as fh:
            json.dump({"candidate": "mycli", "status": "PASS", "pass": True}, fh)

        rows = []
        for trial in (1, 2):
            rows.append(_row("null", "alpha", trial, False))
            rows.append(_row("null", "beta", trial, False))
            rows.append(_row("pi", "alpha", trial, True))
            rows.append(_row("pi", "beta", trial, True))
            rows.append(_row("mycli", "alpha", trial, True, candidate="mycli"))
            rows.append(_row("mycli", "beta", trial, trial == 1, candidate="mycli"))
        _write_jsonl(self.results, rows)
        self.scrub_ctx = scrub.build_context(
            user="pubtestuser",
            home="/Users/pubtestuser",
            hostnames=["pubtest-host"],
        )

    def test_sanitize_drops_load_meta_paths(self):
        row = _row("null", "alpha", 1, False)
        row["_source"] = "/Users/pubtestuser/dev/openbench/results.jsonl"
        row["_lineno"] = 7
        cleaned = publish.sanitize_row_for_publish(row)
        self.assertNotIn("_source", cleaned)
        self.assertNotIn("_lineno", cleaned)

    def test_bundle_creation_and_provenance_hash(self):
        provenance = publish.create_bundle(
            self.results,
            self.out,
            candidate_specs=["mycli"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
        )
        self.assertTrue(os.path.isfile(os.path.join(self.out, "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "results.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "provenance.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "README.md")))
        self.assertFalse(os.path.isdir(os.path.join(self.out, "transcripts")))

        with open(os.path.join(self.out, "results.jsonl"), "rb") as fh:
            raw = fh.read()
        self.assertEqual(provenance["results_sha256"], hashlib.sha256(raw).hexdigest())

        task_digests = {t["task"]: t["content_digest"] for t in provenance["tasks"]}
        self.assertEqual(
            task_digests["alpha"],
            publish.task_content_digest(os.path.join(self.tasks, "alpha")),
        )
        with open(os.path.join(self.out, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("mycli", html)
        self.assertIn("highlight", html)
        self.assertIn('data-arm="mycli', html)
        self.assertIn("unmetered", html)
        self.assertIn("Comparison card", html)

    def test_verify_pass_and_tamper_fail(self):
        publish.create_bundle(
            self.results,
            self.out,
            candidate_specs=["mycli"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
        )
        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        self.assertTrue(all(c["status"] == "PASS" for c in checks), checks)
        self.assertEqual(publish.print_verify_report(checks), 0)

        results_path = os.path.join(self.out, "results.jsonl")
        with open(results_path, "rb") as fh:
            data = fh.read()
        # Flip one byte in the file body.
        mutated = bytearray(data)
        mutated[-2] = (mutated[-2] + 1) % 256
        with open(results_path, "wb") as fh:
            fh.write(mutated)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        sha_check = next(c for c in checks if c["name"] == "results_sha256")
        self.assertEqual(sha_check["status"], "FAIL")
        self.assertEqual(publish.print_verify_report(checks), 1)

    def test_pii_refusal(self):
        dirty = os.path.join(self.tmp.name, "dirty.jsonl")
        rows = [
            _row("null", "alpha", 1, False),
            _row("mycli", "alpha", 1, True, candidate="mycli",
                 error="contact me at leak@example.com for keys"),
        ]
        _write_jsonl(dirty, rows)
        out = os.path.join(self.tmp.name, "pii-bundle")
        with self.assertRaises(publish.PublishError) as ctx:
            publish.create_bundle(
                dirty,
                out,
                candidate_specs=["mycli"],
                tasks_dirs=[self.tasks],
                gate_search_dirs=[self.gate_dir],
                scrub_ctx=self.scrub_ctx,
            )
        self.assertIn("PII", str(ctx.exception))

        # Override path proceeds with a warning.
        provenance = publish.create_bundle(
            dirty,
            out,
            candidate_specs=["mycli"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
            allow_pii_override=True,
        )
        self.assertTrue(os.path.isfile(os.path.join(out, "index.html")))
        with open(os.path.join(out, "results.jsonl"), "rb") as fh:
            raw = fh.read()
        self.assertEqual(provenance["results_sha256"], hashlib.sha256(raw).hexdigest())

    def test_transcript_path_refusal(self):
        dirty = os.path.join(self.tmp.name, "tx.jsonl")
        rows = [
            _row("null", "alpha", 1, False),
            _row("mycli", "alpha", 1, True, candidate="mycli",
                 transcript_path="/tmp/transcripts/cell.txt"),
        ]
        _write_jsonl(dirty, rows)
        with self.assertRaises(publish.PublishError) as ctx:
            publish.create_bundle(
                dirty,
                os.path.join(self.tmp.name, "tx-bundle"),
                tasks_dirs=[self.tasks],
                gate_search_dirs=[self.gate_dir],
                scrub_ctx=self.scrub_ctx,
            )
        self.assertIn("LOCAL-ONLY", str(ctx.exception))

    def test_unmatched_arm_and_missing_gate_warnings(self):
        mismatched = os.path.join(self.tmp.name, "mismatch.jsonl")
        rows = [
            _row("null", "alpha", 1, False),
            _row("null", "alpha", 2, False),
            # Candidate only ran alpha trial 1 — unmatched vs null.
            _row("orphan", "alpha", 1, True, candidate="orphan"),
            _row("orphan", "beta", 1, True, candidate="orphan"),
        ]
        _write_jsonl(mismatched, rows)
        provenance = publish.create_bundle(
            mismatched,
            os.path.join(self.tmp.name, "warn-bundle"),
            candidate_specs=["orphan"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],  # no orphan record
            scrub_ctx=self.scrub_ctx,
        )
        joined = " ".join(provenance["warnings"])
        self.assertIn("different task sets", joined)
        self.assertIn("no candidate-gate PASS record", joined)
        self.assertIn("orphan", joined)
        warn_html = os.path.join(self.tmp.name, "warn-bundle", "index.html")
        with open(warn_html, encoding="utf-8") as fh:
            self.assertIn("Comparability warning", fh.read())

    def test_cli_publish_and_verify_e2e(self):
        out = os.path.join(self.tmp.name, "cli-bundle")
        data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "mycli.json"), "w", encoding="utf-8") as fh:
            json.dump({"candidate": "mycli", "status": "PASS"}, fh)

        proc = subprocess.run(
            [sys.executable, "-m", "obench.cli", "publish",
             "--results-path", self.results,
             "--candidate", "mycli",
             "--out", out,
             "--tasks-dir", self.tasks],
            capture_output=True, text=True,
            cwd=self.tmp.name, env=_SUBPROC_ENV,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("highlight", html)
        self.assertIn("mycli", html)

        verify = subprocess.run(
            [sys.executable, "-m", "obench.cli", "verify", out,
             "--tasks-dir", self.tasks],
            capture_output=True, text=True,
            cwd=self.tmp.name, env=_SUBPROC_ENV,
        )
        self.assertEqual(verify.returncode, 0, msg=verify.stdout + verify.stderr)
        self.assertIn("VERDICT: PASS", verify.stdout)

        results_path = os.path.join(out, "results.jsonl")
        with open(results_path, "ab") as fh:
            fh.write(b" ")
        verify2 = subprocess.run(
            [sys.executable, "-m", "obench.cli", "verify", out,
             "--tasks-dir", self.tasks],
            capture_output=True, text=True,
            cwd=self.tmp.name, env=_SUBPROC_ENV,
        )
        self.assertNotEqual(verify2.returncode, 0)
        self.assertIn("VERDICT: FAIL", verify2.stdout)
        self.assertIn("results_sha256", verify2.stdout)


class TaskDigestTests(unittest.TestCase):
    def test_digest_stable_and_sensitive(self):
        with tempfile.TemporaryDirectory() as td:
            task = _make_task(td, "t")
            a = publish.task_content_digest(task)
            b = publish.task_content_digest(task)
            self.assertEqual(a, b)
            with open(os.path.join(task, "instruction.md"), "a", encoding="utf-8") as fh:
                fh.write("\nextra\n")
            self.assertNotEqual(a, publish.task_content_digest(task))


if __name__ == "__main__":
    unittest.main()
