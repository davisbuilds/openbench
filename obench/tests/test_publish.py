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
        "tokens": 120,
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
        # Manifest candidates do not self-report tokens unless the test sets them.
        if "tokens" not in extra:
            row["tokens"] = None
            row["tokens_input_uncached"] = None
            row["tokens_output"] = None
            row["tokens_cache_read"] = None
    row.update(extra)
    return row


def _harbor_row(
    *,
    openbench_digest="c" * 64,
    harbor_digest="a" * 64,
    **extra,
):
    digest = "a" * 64
    row = _row("codex", "alpha", 1, True)
    row.update({
        "exec_mode": "harbor",
        "tokens": 115,
        "tokens_input_uncached": 75,
        "tokens_cache_read": 25,
        "tokens_cache_write": None,
        "tokens_output": 40,
        "tokens_reasoning": None,
        "tokens_fresh": 115,
        "usage_raw": {
            "source": "harbor_agent_result",
            "n_input_tokens": 100,
            "n_cache_tokens": 25,
            "n_output_tokens": 40,
            "cost_usd": 0.5,
        },
        "token_basis": "harbor_agent_reported",
        "candidate_provenance": {
            "kind": "harbor_job",
            "harbor_version": "0.20.0",
            "harbor_git_commit_hash": "b" * 40,
            "harbor_job_id": "job-1",
            "harbor_trial_id": "trial-1",
            "harbor_trial_name": "alpha__trial-1",
            "job_lock_sha256": digest,
            "job_result_sha256": digest,
            "trial_lock_sha256": digest,
            "trial_result_sha256": digest,
            "reward_sha256": digest,
            "openbench_verifier_evidence_sha256": digest,
            "atif_sha256": digest,
            "artifact_manifest_sha256": digest,
            "final_workspace_sha256": digest,
            "task_digest": "sha256:" + harbor_digest,
            "openbench_task_content_digest": {
                "scheme": 2,
                "sha256": openbench_digest,
            },
            "openbench_harbor_export": {
                "schema_version": 1,
                "base_image": "python:3.11-slim",
                "network_mode": "no-network",
            },
            "harbor_task_checksum": digest,
            "harbor_agent_config_name": "codex__model-x__openbench",
            "harbor_verifier_time_s": 1.25,
            "usage_source": "harbor_agent_reported",
            "proxy_measured": False,
            "trial_mapping": "lexicographic_name_within_task_agent_model",
            "temporal_matched_block_claim": False,
        },
        "workspace_source": {
            "kind": "harbor_artifact",
            "sha256": digest,
        },
    })
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
        self.alpha_digest = publish.task_content_digest(
            os.path.join(self.tasks, "alpha"),
            scheme=publish.DIGEST_SCHEME_CURRENT,
        )
        self.alpha_harbor_digest = publish._canonical_harbor_export_digest(
            os.path.join(self.tasks, "alpha"),
            "alpha",
            {
                "schema_version": 1,
                "base_image": "python:3.11-slim",
                "network_mode": "no-network",
            },
        )
        self.results = os.path.join(self.tmp.name, "results.jsonl")
        self.out = os.path.join(self.tmp.name, "bundle")
        self.gate_dir = os.path.join(self.tmp.name, "gate")
        os.makedirs(self.gate_dir)
        with open(os.path.join(self.gate_dir, "mycli-gate.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "candidate": "mycli", "mode": "live",
                "status": "PASS", "pass": True,
                "candidate_digest": hashlib.sha256(
                    b"mycli-spec").hexdigest(),
                "model": "model-x", "version": "1.0",
            }, fh)

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

    def _harbor_row(self, **extra):
        return _harbor_row(
            openbench_digest=self.alpha_digest,
            harbor_digest=self.alpha_harbor_digest,
            **extra,
        )

    def test_sanitize_drops_load_meta_paths(self):
        row = _row("null", "alpha", 1, False)
        row["_source"] = "/Users/pubtestuser/dev/openbench/results.jsonl"
        row["_lineno"] = 7
        cleaned = publish.sanitize_row_for_publish(row)
        self.assertNotIn("_source", cleaned)
        self.assertNotIn("_lineno", cleaned)

    def test_harbor_publish_binds_safe_evidence_and_drops_private_fields(self):
        harbor_results = os.path.join(self.tmp.name, "harbor.jsonl")
        row = self._harbor_row(
            trajectory_path="/private/job/agent/trajectory.json",
            session_path="/private/session.json",
            transcript_blob="private transcript",
            workspace_path="/private/job/artifacts/workspace",
            credential_material="secret-token",
        )
        _write_jsonl(harbor_results, [row])

        provenance = publish.create_bundle(
            harbor_results,
            self.out,
            tasks_dirs=[self.tasks],
            scrub_ctx=self.scrub_ctx,
        )
        evidence = provenance["harbor_import_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(
            evidence[0]["candidate_provenance"]["atif_sha256"], "a" * 64
        )
        self.assertEqual(
            evidence[0]["candidate_provenance"][
                "openbench_verifier_evidence_sha256"
            ],
            "a" * 64,
        )
        self.assertEqual(
            evidence[0]["candidate_provenance"]["final_workspace_sha256"],
            "a" * 64,
        )
        self.assertEqual(
            evidence[0]["candidate_provenance"][
                "openbench_task_content_digest"
            ],
            {"scheme": 2, "sha256": self.alpha_digest},
        )
        self.assertEqual(
            evidence[0]["candidate_provenance"]["task_digest"],
            "sha256:" + self.alpha_harbor_digest,
        )
        self.assertEqual(
            evidence[0]["usage"]["token_basis"], "harbor_agent_reported"
        )

        with open(os.path.join(self.out, "results.jsonl"), encoding="utf-8") as fh:
            published_row = json.loads(fh.readline())
        for key in (
            "trajectory_path", "session_path", "transcript_blob",
            "workspace_path", "credential_material",
        ):
            self.assertNotIn(key, published_row)
        serialized = json.dumps(provenance) + json.dumps(published_row)
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("private transcript", serialized)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        harbor_check = next(
            item for item in checks if item["name"] == "harbor_import_evidence"
        )
        self.assertEqual(harbor_check["status"], "PASS", harbor_check)

    def test_harbor_publish_rejects_partial_or_inconsistent_provenance(self):
        cases = []
        for key in (
            "atif_sha256", "openbench_verifier_evidence_sha256",
            "final_workspace_sha256", "openbench_task_content_digest",
            "openbench_harbor_export", "usage_source",
        ):
            row = self._harbor_row()
            del row["candidate_provenance"][key]
            cases.append((f"missing-{key}", row))
        row = self._harbor_row()
        row["candidate_provenance"]["credential_path"] = "/private/credentials"
        cases.append(("extra-private-field", row))
        row = self._harbor_row()
        row["workspace_source"]["sha256"] = "c" * 64
        cases.append(("workspace-digest-mismatch", row))
        row = self._harbor_row()
        row["exec_mode"] = "local"
        row["candidate_provenance"]["kind"] = "manifest"
        row["workspace_source"]["kind"] = "snapshot"
        cases.append(("removed-primary-markers", row))
        cases.append(("missing-harbor-provenance", _row(
            "codex", "alpha", 1, True, exec_mode="harbor"
        )))

        for name, row in cases:
            with self.subTest(name=name):
                path = os.path.join(self.tmp.name, f"{name}.jsonl")
                out = os.path.join(self.tmp.name, f"{name}-bundle")
                _write_jsonl(path, [row])
                with self.assertRaises(publish.PublishError):
                    publish.create_bundle(
                        path,
                        out,
                        tasks_dirs=[self.tasks],
                        scrub_ctx=self.scrub_ctx,
                    )
                self.assertFalse(os.path.exists(out))

    def test_harbor_publish_rejects_executed_task_digest_mismatch(self):
        harbor_results = os.path.join(self.tmp.name, "harbor-mismatch.jsonl")
        _write_jsonl(
            harbor_results,
            [_harbor_row(openbench_digest="d" * 64)],
        )

        with self.assertRaisesRegex(
            publish.PublishError,
            "does not match local publication task",
        ):
            publish.create_bundle(
                harbor_results,
                self.out,
                tasks_dirs=[self.tasks],
                scrub_ctx=self.scrub_ctx,
            )
        self.assertFalse(os.path.exists(self.out))

    def test_harbor_publish_rejects_locked_task_digest_mismatch(self):
        harbor_results = os.path.join(
            self.tmp.name,
            "harbor-task-digest-mismatch.jsonl",
        )
        _write_jsonl(
            harbor_results,
            [
                _harbor_row(
                    openbench_digest=self.alpha_digest,
                    harbor_digest="d" * 64,
                )
            ],
        )

        with self.assertRaisesRegex(
            publish.PublishError,
            "does not match canonical OpenBench export",
        ):
            publish.create_bundle(
                harbor_results,
                self.out,
                tasks_dirs=[self.tasks],
                scrub_ctx=self.scrub_ctx,
            )
        self.assertFalse(os.path.exists(self.out))

    def test_verify_rechecks_executed_task_digest_binding(self):
        harbor_results = os.path.join(self.tmp.name, "harbor-binding.jsonl")
        _write_jsonl(
            harbor_results,
            [self._harbor_row()],
        )
        publish.create_bundle(
            harbor_results,
            self.out,
            tasks_dirs=[self.tasks],
            scrub_ctx=self.scrub_ctx,
        )

        results_path = os.path.join(self.out, "results.jsonl")
        with open(results_path, encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        tampered_digest = "d" * 64
        row["candidate_provenance"]["openbench_task_content_digest"][
            "sha256"
        ] = tampered_digest
        _write_jsonl(results_path, [row])
        with open(results_path, "rb") as fh:
            results_sha = hashlib.sha256(fh.read()).hexdigest()

        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        provenance["results_sha256"] = results_sha
        provenance["harbor_import_evidence"][0]["candidate_provenance"][
            "openbench_task_content_digest"
        ]["sha256"] = tampered_digest
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        harbor_evidence = next(
            item for item in checks if item["name"] == "harbor_import_evidence"
        )
        binding = next(
            item for item in checks
            if item["name"] == "harbor_task_binding:alpha"
        )
        self.assertEqual(harbor_evidence["status"], "PASS", harbor_evidence)
        self.assertEqual(binding["status"], "FAIL", binding)

    def test_verify_rechecks_locked_harbor_task_digest(self):
        harbor_results = os.path.join(
            self.tmp.name,
            "harbor-lock-binding.jsonl",
        )
        _write_jsonl(harbor_results, [self._harbor_row()])
        publish.create_bundle(
            harbor_results,
            self.out,
            tasks_dirs=[self.tasks],
            scrub_ctx=self.scrub_ctx,
        )

        results_path = os.path.join(self.out, "results.jsonl")
        with open(results_path, encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        row["candidate_provenance"]["task_digest"] = "sha256:" + "d" * 64
        _write_jsonl(results_path, [row])
        with open(results_path, "rb") as fh:
            results_sha = hashlib.sha256(fh.read()).hexdigest()

        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        provenance["results_sha256"] = results_sha
        provenance["harbor_import_evidence"][0]["candidate_provenance"][
            "task_digest"
        ] = "sha256:" + "d" * 64
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        harbor_evidence = next(
            item for item in checks if item["name"] == "harbor_import_evidence"
        )
        content_binding = next(
            item for item in checks
            if item["name"] == "harbor_task_binding:alpha"
        )
        export_binding = next(
            item for item in checks
            if item["name"] == "harbor_export_binding:alpha"
        )
        self.assertEqual(harbor_evidence["status"], "PASS", harbor_evidence)
        self.assertEqual(content_binding["status"], "PASS", content_binding)
        self.assertEqual(export_binding["status"], "FAIL", export_binding)

    def test_verify_rejects_harbor_evidence_manifest_tampering(self):
        harbor_results = os.path.join(self.tmp.name, "harbor.jsonl")
        _write_jsonl(
            harbor_results,
            [self._harbor_row()],
        )
        publish.create_bundle(
            harbor_results,
            self.out,
            tasks_dirs=[self.tasks],
            scrub_ctx=self.scrub_ctx,
        )
        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        provenance["harbor_import_evidence"][0]["candidate_provenance"][
            "atif_sha256"
        ] = "c" * 64
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        harbor_check = next(
            item for item in checks if item["name"] == "harbor_import_evidence"
        )
        self.assertEqual(harbor_check["status"], "FAIL", harbor_check)

    def test_verify_rejects_rehashed_partial_harbor_row_without_manifest(self):
        harbor_results = os.path.join(self.tmp.name, "harbor.jsonl")
        _write_jsonl(
            harbor_results,
            [self._harbor_row()],
        )
        publish.create_bundle(
            harbor_results,
            self.out,
            tasks_dirs=[self.tasks],
            scrub_ctx=self.scrub_ctx,
        )
        results_path = os.path.join(self.out, "results.jsonl")
        with open(results_path, encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        del row["candidate_provenance"]["atif_sha256"]
        _write_jsonl(results_path, [row])
        with open(results_path, "rb") as fh:
            results_sha = hashlib.sha256(fh.read()).hexdigest()

        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        provenance["results_sha256"] = results_sha
        del provenance["harbor_import_evidence"]
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        harbor_check = next(
            item for item in checks if item["name"] == "harbor_import_evidence"
        )
        self.assertEqual(harbor_check["status"], "FAIL", harbor_check)
        self.assertIn("malformed candidate_provenance", harbor_check["detail"])

    def test_non_harbor_publish_schema_remains_compatible(self):
        row = _row("null", "alpha", 1, False, safe_extension="kept")
        cleaned = publish.sanitize_row_for_publish(row)
        self.assertEqual(cleaned["safe_extension"], "kept")
        provenance = publish.build_provenance(
            [cleaned],
            "a" * 64,
            tasks_dirs=[self.tasks],
        )
        self.assertNotIn("harbor_import_evidence", provenance)

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

        self.assertEqual(provenance["digest_scheme"], publish.DIGEST_SCHEME_CURRENT)
        task_digests = {t["task"]: t["content_digest"] for t in provenance["tasks"]}
        self.assertEqual(
            task_digests["alpha"],
            publish.task_content_digest(
                os.path.join(self.tasks, "alpha"),
                scheme=publish.DIGEST_SCHEME_CURRENT,
            ),
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

    def test_verify_rejects_duplicate_task_entries(self):
        publish.create_bundle(
            self.results,
            self.out,
            candidate_specs=["mycli"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
        )
        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        provenance["tasks"].append(dict(provenance["tasks"][0]))
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        manifest = next(c for c in checks if c["name"] == "task_manifest")
        self.assertEqual(manifest["status"], "FAIL", manifest)
        self.assertIn("duplicates=", manifest["detail"])

    def test_verify_rejects_non_sha256_task_digest(self):
        publish.create_bundle(
            self.results,
            self.out,
            candidate_specs=["mycli"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
        )
        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        provenance["tasks"][0]["content_digest"] = "not-a-sha256"
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh)

        checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
        manifest = next(c for c in checks if c["name"] == "task_manifest")
        self.assertEqual(manifest["status"], "FAIL", manifest)

    def test_verify_malformed_task_manifest_fails_without_crashing(self):
        publish.create_bundle(
            self.results,
            self.out,
            candidate_specs=["mycli"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
        )
        provenance_path = os.path.join(self.out, "provenance.json")
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
        for malformed in ([1], 1):
            provenance["tasks"] = malformed
            with open(provenance_path, "w", encoding="utf-8") as fh:
                json.dump(provenance, fh)
            checks = publish.verify_bundle(self.out, tasks_dirs=[self.tasks])
            manifest = next(c for c in checks if c["name"] == "task_manifest")
            self.assertEqual(manifest["status"], "FAIL")

    def test_gate_lookup_requires_exact_version_stamp(self):
        digest = hashlib.sha256(b"mycli-spec").hexdigest()
        self.assertIsNone(publish.find_candidate_gate_record(
            "mycli",
            search_dirs=[self.gate_dir],
            candidate_digest=digest,
            model="model-x",
            harness_version=None,
        ))

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

    def test_unmatched_arm_and_missing_gate_require_explicit_override(self):
        mismatched = os.path.join(self.tmp.name, "mismatch.jsonl")
        rows = [
            _row("null", "alpha", 1, False),
            _row("null", "alpha", 2, False),
            # Candidate only ran alpha trial 1 — unmatched vs null.
            _row("orphan", "alpha", 1, True, candidate="orphan"),
            _row("orphan", "beta", 1, True, candidate="orphan"),
        ]
        _write_jsonl(mismatched, rows)
        with self.assertRaises(publish.PublishError):
            publish.create_bundle(
                mismatched,
                os.path.join(self.tmp.name, "blocked-bundle"),
                candidate_specs=["orphan"],
                tasks_dirs=[self.tasks],
                gate_search_dirs=[self.gate_dir],
                scrub_ctx=self.scrub_ctx,
            )
        provenance = publish.create_bundle(
            mismatched,
            os.path.join(self.tmp.name, "warn-bundle"),
            candidate_specs=["orphan"],
            tasks_dirs=[self.tasks],
            gate_search_dirs=[self.gate_dir],
            scrub_ctx=self.scrub_ctx,
            allow_incomplete=True,
        )
        joined = " ".join(provenance["warnings"])
        self.assertIn("different task sets", joined)
        self.assertIn("no matching live candidate-gate PASS record", joined)
        self.assertIn("orphan", joined)
        warn_html = os.path.join(self.tmp.name, "warn-bundle", "index.html")
        with open(warn_html, encoding="utf-8") as fh:
            self.assertIn("Comparability warning", fh.read())

    def test_cli_publish_and_verify_e2e(self):
        out = os.path.join(self.tmp.name, "cli-bundle")
        data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "mycli.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "candidate": "mycli", "mode": "live",
                "status": "PASS", "pass": True,
                "candidate_digest": hashlib.sha256(
                    b"mycli-spec").hexdigest(),
                "model": "model-x", "version": "1.0",
            }, fh)

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

    def test_digest_includes_checker_data(self):
        with tempfile.TemporaryDirectory() as td:
            task = _make_task(td, "t")
            before = publish.task_content_digest(task)
            cd = os.path.join(task, "checker_data")
            os.makedirs(cd)
            with open(os.path.join(cd, "expected.txt"), "w", encoding="utf-8") as fh:
                fh.write("oracle\n")
            after = publish.task_content_digest(task)
            self.assertNotEqual(before, after)
            with open(os.path.join(cd, "expected.txt"), "a", encoding="utf-8") as fh:
                fh.write("changed\n")
            self.assertNotEqual(after, publish.task_content_digest(task))

    def test_legacy_scheme1_ignores_checker_data(self):
        with tempfile.TemporaryDirectory() as td:
            task = _make_task(td, "t")
            legacy = publish.task_content_digest(
                task, scheme=publish.DIGEST_SCHEME_LEGACY
            )
            cd = os.path.join(task, "checker_data")
            os.makedirs(cd)
            with open(os.path.join(cd, "expected.txt"), "w", encoding="utf-8") as fh:
                fh.write("oracle\n")
            self.assertEqual(
                legacy,
                publish.task_content_digest(
                    task, scheme=publish.DIGEST_SCHEME_LEGACY
                ),
            )
            self.assertNotEqual(
                legacy,
                publish.task_content_digest(
                    task, scheme=publish.DIGEST_SCHEME_CURRENT
                ),
            )

    def test_verify_legacy_bundle_without_scheme_field(self):
        """Pre-digest_scheme bundles hash without checker_data/ (scheme 1)."""
        with tempfile.TemporaryDirectory() as td:
            tasks = os.path.join(td, "tasks")
            os.makedirs(tasks)
            task = _make_task(tasks, "alpha")
            cd = os.path.join(task, "checker_data")
            os.makedirs(cd)
            with open(os.path.join(cd, "expected.txt"), "w", encoding="utf-8") as fh:
                fh.write("oracle\n")
            legacy_digest = publish.task_content_digest(
                task, scheme=publish.DIGEST_SCHEME_LEGACY
            )
            current_digest = publish.task_content_digest(
                task, scheme=publish.DIGEST_SCHEME_CURRENT
            )
            self.assertNotEqual(legacy_digest, current_digest)

            bundle = os.path.join(td, "bundle")
            os.makedirs(bundle)
            results = os.path.join(bundle, "results.jsonl")
            _write_jsonl(results, [_row("null", "alpha", 1, False)])
            with open(results, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            # No digest_scheme field → legacy scheme 1.
            provenance = {
                "results_sha256": sha,
                "tasks": [{"task": "alpha", "content_digest": legacy_digest}],
            }
            with open(os.path.join(bundle, "provenance.json"), "w", encoding="utf-8") as fh:
                json.dump(provenance, fh)

            checks = publish.verify_bundle(bundle, tasks_dirs=[tasks])
            digest_check = next(c for c in checks if c["name"] == "task_digest:alpha")
            self.assertEqual(digest_check["status"], "PASS", digest_check)
            self.assertIn("scheme=1", digest_check["detail"])

    def test_publish_records_scheme2_covering_checker_data(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = os.path.join(td, "tasks")
            os.makedirs(tasks)
            task = _make_task(tasks, "alpha")
            cd = os.path.join(task, "checker_data")
            os.makedirs(cd)
            with open(os.path.join(cd, "expected.txt"), "w", encoding="utf-8") as fh:
                fh.write("oracle\n")
            results = os.path.join(td, "results.jsonl")
            _write_jsonl(results, [_row("null", "alpha", 1, False)])
            out = os.path.join(td, "bundle")
            scrub_ctx = scrub.build_context(
                user="pubtestuser",
                home="/Users/pubtestuser",
                hostnames=["pubtest-host"],
            )
            provenance = publish.create_bundle(
                results,
                out,
                tasks_dirs=[tasks],
                scrub_ctx=scrub_ctx,
            )
            self.assertEqual(provenance["digest_scheme"], publish.DIGEST_SCHEME_CURRENT)
            recorded = next(t["content_digest"] for t in provenance["tasks"]
                            if t["task"] == "alpha")
            self.assertEqual(
                recorded,
                publish.task_content_digest(
                    task, scheme=publish.DIGEST_SCHEME_CURRENT
                ),
            )
            self.assertNotEqual(
                recorded,
                publish.task_content_digest(
                    task, scheme=publish.DIGEST_SCHEME_LEGACY
                ),
            )
            checks = publish.verify_bundle(out, tasks_dirs=[tasks])
            digest_check = next(c for c in checks if c["name"] == "task_digest:alpha")
            self.assertEqual(digest_check["status"], "PASS", digest_check)
            self.assertIn("scheme=2", digest_check["detail"])

            # Tampering checker_data must fail scheme-2 verify.
            with open(os.path.join(cd, "expected.txt"), "a", encoding="utf-8") as fh:
                fh.write("tampered\n")
            checks = publish.verify_bundle(out, tasks_dirs=[tasks])
            digest_check = next(c for c in checks if c["name"] == "task_digest:alpha")
            self.assertEqual(digest_check["status"], "FAIL")
            self.assertIn("scheme=2", digest_check["detail"])

    def test_verify_fails_missing_content_digest(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = os.path.join(td, "tasks")
            os.makedirs(tasks)
            _make_task(tasks, "alpha")
            bundle = os.path.join(td, "bundle")
            os.makedirs(bundle)
            results = os.path.join(bundle, "results.jsonl")
            _write_jsonl(results, [_row("null", "alpha", 1, False)])
            with open(results, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            provenance = {
                "results_sha256": sha,
                "tasks": [{"task": "alpha", "content_digest": None}],
            }
            with open(os.path.join(bundle, "provenance.json"), "w", encoding="utf-8") as fh:
                json.dump(provenance, fh)
            checks = publish.verify_bundle(bundle, tasks_dirs=[tasks])
            digest_check = next(c for c in checks if c["name"] == "task_digest:alpha")
            self.assertEqual(digest_check["status"], "FAIL")
            self.assertIn("no content_digest", digest_check["detail"])


if __name__ == "__main__":
    unittest.main()
