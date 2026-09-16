"""Trial evidence uses real files and checkers; only harness execution is stubbed."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from obench import run
from obench.publish import sanitize_row_for_publish


class TrialEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = self.root / "tasks" / "task"
        (self.task / "workspace").mkdir(parents=True)
        (self.task / "instruction.md").write_text("Repair the fixture.\n")
        checker = self.task / "checker.sh"
        checker.write_text("#!/bin/sh\necho checker-ran\nexit 0\n")
        checker.chmod(0o755)
        self.result = {
            "completed": True, "full_output": "private raw stream\n",
            "final_message": "private final response", "tool_events": [
                {"type": "tool_use", "name": "Read", "input": {"path": "skill.md"}}],
        }

    def cell(self, *, required=False, transcripts=True, result=None,
             captured=False, exec_mode="local", proxy_ctx=None, adapter_error=None):
        candidate = SimpleNamespace(
            identity_digest="a" * 64, base_adapter="null", proxy_adapter=None,
            kind="config", provenance=None, REQUIRE_EVIDENCE=required,
            captured_context=captured,
        )
        # A live model is neither necessary nor authorized for this I/O contract.
        with mock.patch.object(run, "invoke_adapter", side_effect=adapter_error, return_value=(
                self.result if result is None else result, "local")):
            return run.run_cell(
                "candidate", "task", "model", 1, 10, str(self.task.parent),
                run.DEFAULT_ADAPTERS_DIR, 10, candidate=candidate,
                transcripts_dir=str(self.root / "transcripts") if transcripts else None,
                results_stem="screen", exec_mode=exec_mode, proxy_ctx=proxy_ctx,
            )

    def test_complete_bundle_is_trial_linked_hashed_private_and_published_without_payload(self):
        row = self.cell(required=True)
        self.assertEqual(row["evidence_status"], "complete")
        path = Path(run.evidence_path(self.root / "transcripts", "screen", row["run_id"],
                                      row["evidence_attempt_id"]))
        data = path.read_bytes()
        bundle = json.loads(data)
        self.assertEqual(bundle["run_id"], row["run_id"])
        self.assertEqual(bundle["full_output"], self.result["full_output"])
        self.assertEqual(bundle["final_message"], self.result["final_message"])
        self.assertEqual(bundle["tool_events"], self.result["tool_events"])
        self.assertEqual(bundle["evidence_status"], "complete")
        for key in ("full_output", "final_message", "tool_events", "evidence_error"):
            component = (json.dumps(bundle[key], sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")) + "\n").encode("utf-8")
            self.assertEqual(bundle["sha256"][key], hashlib.sha256(component).hexdigest())
        self.assertEqual(row["evidence_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        output = self.root / "results.jsonl"
        run.append_row(str(output), row)
        saved = json.loads(output.read_text())
        self.assertEqual(saved["evidence_status"], "complete")
        self.assertEqual(saved["evidence_attempt_id"], row["evidence_attempt_id"])
        self.assertEqual(saved["evidence_sha256"], row["evidence_sha256"])
        public = json.dumps(sanitize_row_for_publish(saved))
        self.assertNotIn("private raw", public)
        self.assertNotIn("private final", public)
        self.assertNotIn(str(path), public)
        self.assertTrue(row["success"])
        self.assertNotEqual(row["failure_class"], "infra")

    def test_required_partial_evidence_excludes_trial_but_keeps_checker_verdict(self):
        for missing in ("full_output", "final_message", "tool_events"):
            with self.subTest(missing=missing):
                result = dict(self.result)
                del result[missing]
                # Give each case its own storage to avoid legitimate collision detection.
                with tempfile.TemporaryDirectory(dir=self.tmp.name) as directory:
                    old = self.root
                    self.root = Path(directory)
                    try:
                        row = self.cell(required=True, result=result)
                    finally:
                        self.root = old
                self.assertEqual(row["evidence_status"], "partial")
                self.assertEqual(row["failure_class"], "infra")
                self.assertTrue(row["success"])
                self.assertEqual(row["score"], 1.0)
                self.assertEqual(row["checker_stdout"], "checker-ran\n")

    def test_adapter_parse_failure_is_partial_and_error_details_stay_local(self):
        row = self.cell(required=True, result={
            **self.result, "evidence_error": "private parser exception /Users/example"})
        self.assertEqual(row["evidence_status"], "partial")
        self.assertEqual(row["evidence_error_code"], "adapter_evidence_error")
        self.assertEqual(row["failure_class"], "infra")
        self.assertNotIn("private parser", json.dumps(row))

    def test_required_evidence_disabled_is_explicit_infra(self):
        row = self.cell(required=True, transcripts=False)
        self.assertEqual(row["evidence_status"], "disabled")
        self.assertEqual(row["transcript_status"], "disabled")
        self.assertEqual(row["failure_class"], "infra")
        self.assertEqual(row["checker_exit"], 0)

    def test_early_adapter_failure_records_missing_required_evidence(self):
        row = self.cell(required=True, adapter_error=RuntimeError("fixture adapter failure"))
        self.assertEqual(row["evidence_status"], "missing")
        self.assertEqual(row["transcript_status"], "missing")
        self.assertTrue(row["evidence_required"])
        self.assertEqual(row["failure_class"], "infra")

    def test_captured_docker_and_proxy_are_refused_before_adapter_or_checker(self):
        for options, reason in (({"exec_mode": "docker"}, "captured_context_execution_mode"),
                                ({"proxy_ctx": {"unusable": True}}, "captured_context_proxy")):
            with self.subTest(reason=reason):
                row = self.cell(required=True, captured=True,
                                adapter_error=AssertionError("must not execute"), **options)
                self.assertEqual(row["failure_reason"], reason)
                self.assertEqual(row["failure_class"], "infra")
                self.assertIsNone(row["checker_exit"])

    def test_symlink_destination_is_refused_without_overwriting_target(self):
        outside = self.root / "outside"
        outside.write_text("original")
        destination = self.root / "evidence.json"
        destination.symlink_to(outside)
        with self.assertRaises(OSError):
            run.write_trial_evidence(str(destination), {"run_id": "trial"}, self.result)
        self.assertEqual(outside.read_text(), "original")

    def test_real_storage_collision_is_reported_without_losing_checker_outcome(self):
        (self.root / "transcripts").write_text("occupied")
        row = self.cell(required=True)
        self.assertEqual(row["evidence_status"], "failed")
        self.assertEqual(row["evidence_error_code"], "persistence_error")
        self.assertEqual(row["transcript_status"], "failed")
        self.assertEqual(row["transcript_error_code"], "persistence_error")
        self.assertEqual(row["failure_class"], "infra")
        self.assertEqual(row["checker_exit"], 0)
        self.assertIsNone(row["evidence_sha256"])
        self.assertEqual((self.root / "transcripts").read_text(), "occupied")

    def test_legacy_transcript_symlink_failure_does_not_invalidate_complete_bundle(self):
        outside = self.root / "outside"
        outside.write_text("original target bytes")
        run_id = run.make_run_id("candidate", "task", "model", 1, "a" * 64)
        legacy = Path(run.transcript_path(self.root / "transcripts", "screen", run_id))
        legacy.parent.mkdir(parents=True)
        legacy.symlink_to(outside)

        row = self.cell(required=True)
        self.assertEqual(row["evidence_status"], "complete")
        self.assertIsNone(row["evidence_error_code"])
        self.assertEqual(row["transcript_status"], "failed")
        self.assertEqual(row["transcript_error_code"], "persistence_error")
        self.assertNotEqual(row["failure_class"], "infra")
        self.assertEqual(row["checker_exit"], 0)
        bundle = Path(run.evidence_path(self.root / "transcripts", "screen", row["run_id"],
                                        row["evidence_attempt_id"]))
        self.assertEqual(row["evidence_sha256"], hashlib.sha256(bundle.read_bytes()).hexdigest())
        self.assertEqual(outside.read_text(), "original target bytes")
        output = self.root / "results.jsonl"
        run.append_row(str(output), row)
        saved = json.loads(output.read_text())
        self.assertEqual(saved["transcript_status"], "failed")
        self.assertEqual(saved["transcript_error_code"], "persistence_error")

        # Positive control: removing only the obstructing symlink restores the copy.
        legacy.unlink()
        repaired = self.cell(required=True)
        self.assertEqual(repaired["evidence_status"], "complete")
        self.assertEqual(repaired["transcript_status"], "complete")
        self.assertIsNone(repaired["transcript_error_code"])
        self.assertIn(self.result["full_output"], legacy.read_text())
        self.assertEqual(outside.read_text(), "original target bytes")

    def test_optional_failure_is_detectable_without_changing_checker_grading(self):
        (self.root / "transcripts").write_text("occupied")
        row = self.cell()
        self.assertEqual(row["evidence_status"], "failed")
        self.assertEqual(row["transcript_status"], "failed")
        self.assertEqual(row["transcript_error_code"], "persistence_error")
        self.assertTrue(row["success"])
        self.assertNotEqual(row["failure_class"], "infra")

    def test_identical_bundle_write_is_idempotent_and_conflicting_write_is_refused(self):
        row = self.cell()
        path = run.evidence_path(self.root / "transcripts", "screen", row["run_id"],
                                 row["evidence_attempt_id"])
        original = Path(path).read_bytes()
        self.assertEqual(run.write_trial_evidence(path, row, self.result), row["evidence_sha256"])
        with self.assertRaises(FileExistsError):
            run.write_trial_evidence(path, row, {**self.result, "final_message": "different"})
        self.assertEqual(Path(path).read_bytes(), original)
        self.assertEqual(sorted(p.name for p in Path(path).parent.iterdir()),
                         sorted([Path(path).name, Path(run.transcript_path(
                             self.root / "transcripts", "screen", row["run_id"])).name]))

    def test_repeated_cell_retains_both_attempts_without_evidence_collision(self):
        first = self.cell(required=True)
        second = self.cell(required=True, result={
            **self.result, "full_output": "second attempt stream",
            "final_message": "second attempt answer"})
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["evidence_status"], "complete")
        self.assertNotEqual(first["evidence_attempt_id"], second["evidence_attempt_id"])
        artifacts = []
        for row in (first, second):
            path = Path(run.evidence_path(self.root / "transcripts", "screen",
                                          row["run_id"], row["evidence_attempt_id"]))
            bundle = json.loads(path.read_bytes())
            self.assertEqual(bundle["evidence_attempt_id"], row["evidence_attempt_id"])
            artifacts.append(bundle)
            self.assertNotEqual(row["failure_class"], "infra")
        self.assertEqual(artifacts[0]["final_message"], self.result["final_message"])
        self.assertEqual(artifacts[1]["final_message"], "second attempt answer")

    def test_partial_first_attempt_cannot_invalidate_complete_retry(self):
        first = self.cell(required=True, result={**self.result, "final_message": None})
        second = self.cell(required=True)
        self.assertEqual(first["evidence_status"], "partial")
        self.assertEqual(second["evidence_status"], "complete")
        self.assertNotEqual(second["failure_class"], "infra")

    def test_run_id_sanitization_collision_cannot_alias_evidence(self):
        self.assertNotEqual(run.evidence_path("/tmp", "screen", "a:b"),
                            run.evidence_path("/tmp", "screen", "a_b"))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permission denial")
    def test_readonly_parent_denies_write_and_restoring_permissions_allows_it(self):
        denied = self.root / "denied"
        denied.mkdir(mode=0o500)
        path = denied / "new" / "trial.evidence.json"
        row = {"run_id": "trial", "harness": "fixture", "model": "model", "task": "task", "trial": 1}
        try:
            with self.assertRaises(PermissionError):
                run.write_trial_evidence(str(path), row, self.result)
        finally:
            denied.chmod(0o700)
        self.assertFalse(path.exists())
        self.assertTrue(run.write_trial_evidence(str(path), row, self.result))


if __name__ == "__main__":
    unittest.main()
