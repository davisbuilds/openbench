"""Sandbox policy survives canonical suite manifest validation, locally only."""
import copy
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from obench import init, stats, suite_run
from obench.harbor_job import canonical_comparison_plan_bytes
from obench.tests import test_suite_run


class SandboxManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        init.init_scaffold(root)
        path = root / ".openbench/suites/default.toml"
        cls.ordinary = suite_run.compile_suite(path).manifest
        task_root = root / ".openbench/tasks"
        shutil.rmtree(task_root)
        source = Path(__file__).resolve().parents[2] / "harbor-tasks-local/dojo-evidence-pr60-v3"
        shutil.copytree(source, task_root / source.name)
        path.write_text(
            path.read_text().replace("gpt-5.6-sol", "gpt-5.6-terra-xhigh")
            + '\n[sandbox]\nkind = "repair-v1"\n'
            + 'runtime_image = "sha256:' + "a" * 64 + '"\nmax_requests = 37\n'
        )
        compiled = suite_run.compile_suite(path)
        cls.sandbox = compiled.manifest
        job = suite_run.plan_jobs(compiled)[0]
        plan_path = root / "comparison-plan.json"
        plan_path.write_bytes(canonical_comparison_plan_bytes(job.artifact.comparison_plan.as_dict()))
        cls.rows = suite_run._bind_suite_provenance(
            compiled, job.task_set_id,
            test_suite_run.SuiteRunTests()._simulated_rows(plan_path),
        )
        for row in cls.rows:
            row["candidate_provenance"].update({
                "sandbox_grading_sha256": "b" * 64,
                "sandbox_gateway_module_sha256": cls.sandbox["sandbox"]["implementation_sha256"]["obench.sandbox_gateway"],
            })

    def validate(self, manifest):
        # Recompute the seal: malformed shapes must fail independently of tampering.
        digest = hashlib.sha256(stats._canonical_suite_manifest_bytes(manifest)).hexdigest()
        stats._validate_suite_manifest_shape(manifest, digest)

    def test_real_compiled_manifests_are_accepted(self):
        self.assertNotIn("sandbox", self.ordinary)
        self.validate(self.ordinary)
        self.validate(self.sandbox)

    def test_historical_manifest_without_request_timeout_remains_valid(self):
        manifest = copy.deepcopy(self.sandbox)
        del manifest["sandbox"]["request_timeout_seconds"]
        self.validate(manifest)

    def test_valid_policy_boundaries_and_named_image_digest(self):
        for limit in (1, 1000):
            manifest = copy.deepcopy(self.sandbox)
            manifest["sandbox"]["max_requests"] = limit
            manifest["sandbox"]["runtime_image"] = "registry.example:5000/image@sha256:" + "f" * 64
            self.validate(manifest)

    def test_rejects_invalid_sandbox_policy(self):
        changes = {
            "kind": ("repair-v2", None),
            "runtime_image": ("image:latest", "sha256:" + "a" * 63, "sha256:" + "A" * 64, None),
            "max_requests": (0, 1001, True, 1.0, "1", None),
            "request_timeout_seconds": (0, -1, True, "1200", None, 180),
            "implementation_sha256": ({}, [], None),
        }
        for key, values in changes.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    manifest = copy.deepcopy(self.sandbox)
                    manifest["sandbox"][key] = value
                    with self.assertRaisesRegex(ValueError, "sandbox"):
                        self.validate(manifest)

    def test_rejects_missing_extra_and_malformed_policy_fields(self):
        policies = [None, [], {}, {**self.sandbox["sandbox"], "upstream": "example.com"}]
        policies.extend({key: value for key, value in self.sandbox["sandbox"].items() if key != omitted}
                        for omitted in self.sandbox["sandbox"] if omitted != "request_timeout_seconds")
        for policy in policies:
            with self.subTest(policy=policy):
                manifest = copy.deepcopy(self.sandbox)
                manifest["sandbox"] = policy
                with self.assertRaisesRegex(ValueError, "sandbox"):
                    self.validate(manifest)

    def test_rejects_incomplete_extra_and_invalid_implementation_hashes(self):
        original = self.sandbox["sandbox"]["implementation_sha256"]
        for name in original:
            for value in ("", "a" * 63, "A" * 64, "g" * 64, None, 7):
                with self.subTest(name=name, value=value):
                    manifest = copy.deepcopy(self.sandbox)
                    manifest["sandbox"]["implementation_sha256"][name] = value
                    with self.assertRaisesRegex(ValueError, "sandbox"):
                        self.validate(manifest)
        for hashes in ({key: value for key, value in original.items() if key != next(iter(original))},
                       {**original, "obench.extra": "a" * 64}):
            manifest = copy.deepcopy(self.sandbox)
            manifest["sandbox"]["implementation_sha256"] = hashes
            with self.assertRaisesRegex(ValueError, "sandbox"):
                self.validate(manifest)

    def test_rejects_sandbox_publication_outside_local_only(self):
        for publication in ({"scope": "public", "completeness": "complete"},
                            {"scope": "unknown"}, {}, None, []):
            with self.subTest(publication=publication):
                manifest = copy.deepcopy(self.sandbox)
                manifest["publication"] = publication
                with self.assertRaisesRegex(ValueError, "sandbox"):
                    self.validate(manifest)

    def test_ordinary_unknown_fields_remain_rejected(self):
        manifest = copy.deepcopy(self.ordinary)
        manifest["unknown"] = {}
        with self.assertRaisesRegex(ValueError, "unexpected or missing fields"):
            self.validate(manifest)

    def test_reward_rows_bind_gateway_code_to_sealed_manifest(self):
        stats.validate_suite_rows(self.rows)
        for value in (None, "", "0" * 64):
            rows = copy.deepcopy(self.rows)
            rows[0]["candidate_provenance"]["sandbox_gateway_module_sha256"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "sandbox gateway"):
                stats.validate_suite_rows(rows)

    def test_graded_rows_require_a_well_formed_grading_receipt_digest(self):
        stats.validate_suite_rows(self.rows)
        for invalid in (None, "", "x", "a" * 63, "a" * 65, "g" * 64, "A" * 64, 64, True):
            rows = copy.deepcopy(self.rows)
            rows[0]["candidate_provenance"]["sandbox_grading_sha256"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "grading receipt"):
                stats.validate_suite_rows(rows)
        rows = copy.deepcopy(self.rows)
        del rows[0]["candidate_provenance"]["sandbox_grading_sha256"]
        with self.assertRaisesRegex(ValueError, "grading receipt"):
            stats.validate_suite_rows(rows)

    def test_ungraded_rows_validate_receipt_digest_when_present(self):
        rows = copy.deepcopy(self.rows)
        rows[0].update(score=None, completed=False, success=False, failure_class="infra")
        stats.validate_suite_rows(rows)
        for invalid in (None, "", "not-a-digest", "g" * 64, False):
            rows[0]["candidate_provenance"]["sandbox_grading_sha256"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "grading receipt"):
                stats.validate_suite_rows(rows)

    def test_ungraded_failures_may_lack_receipt_but_cannot_forge_one(self):
        rows = copy.deepcopy(self.rows)
        row = rows[0]
        row.update(score=None, completed=False, success=False, failure_class="infra")
        provenance = row["candidate_provenance"]
        provenance.pop("sandbox_grading_sha256")
        provenance.pop("sandbox_gateway_module_sha256")
        stats.validate_suite_rows(rows)
        provenance["sandbox_grading_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "sandbox gateway"):
            stats.validate_suite_rows(rows)
