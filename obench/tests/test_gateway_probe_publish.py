import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from obench import (
    gateway_probe_publish,
    gateway_probe_report,
    gateway_probe_results,
    gateway_probe_run,
    gateway_probe_spec,
    gateway_spec,
)
from obench.gateway_probe_models import GatewayProbeRunError
from obench.tests.test_gateway_probe_report import row
from obench.tests.test_gateway_probe_spec import manifest


TEST_COMMIT = subprocess.check_output(
    ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"],
    text=True,
).strip()


def _json(value):
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_manifest_hash(bundle, name):
    path = bundle / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["files"][name]["sha256"] = _sha256(bundle / name)
    path.write_text(_json(value), encoding="ascii")


def build_private_run(root, *, prompt="PRIVATE PROMPT must never publish"):
    run_dir = Path(root, "private-run")
    run_dir.mkdir()
    experiment_text = manifest(prompt)
    (run_dir / "experiment.toml").write_text(experiment_text, encoding="utf-8")
    experiment = gateway_probe_spec.load_experiment(run_dir / "experiment.toml")
    prices = {
        "schema_version": 1,
        "price_id": "frozen-env-v1",
        "currency": "USD",
        "prices": [{
            "model": "openai/gpt-4o-mini",
            "input_per_million": "1",
            "output_per_million": "2",
            "effective_at": "2026-07-25T00:00:00Z",
            "currency": "USD",
        }],
    }
    price_digest = gateway_spec.canonical_digest(prices)
    schedule = gateway_probe_run.build_schedule(experiment)
    schedule_digest = gateway_spec.canonical_digest([
        {
            "case_id": block.case_id,
            "prompt_digest": block.prompt_digest,
            "condition": block.condition,
            "repetition": block.repetition,
            "arm_ids": list(block.arm_ids),
        }
        for block in schedule
    ])
    blocks = {block.coordinate: block for block in schedule}
    arms = {arm.arm_id: arm for arm in experiment.arms}
    rows = [
        row(arm_id, condition, repetition, baseline=arm_id == "direct")
        for repetition in (1, 2)
        for condition in ("cold", "warm")
        for arm_id in ("direct", "gateway")
    ]
    for item in rows:
        arm_id = item["identity"]["arm"]["id"]
        item["identity"]["experiment"] = {
            "id": experiment.experiment_id,
            "digest": experiment.digest,
        }
        item["identity"]["arm"]["digest"] = arms[arm_id].digest
        item["identity"]["case"] = {
            "id": experiment.cases[0].case_id,
            "prompt_digest": experiment.cases[0].prompt_digest,
        }
        item["identity"]["comparison"]["price_digest"] = price_digest
        item["identity"]["comparison"]["schedule_digest"] = schedule_digest
        schedule_identity = item["identity"]["schedule"]
        block = blocks[(
            experiment.cases[0].case_id,
            schedule_identity["condition"],
            schedule_identity["repetition"],
        )]
        schedule_identity["block_id"] = gateway_probe_results.block_id(
            experiment.digest,
            block,
            schedule_identity["block_attempt"],
        )
        item["scheduled_blocks_per_condition"] = experiment.repetitions
        item["model_match"] = experiment.model_match
        item["request_metrics"]["route"]["gateway_metadata"] = {
            "generationId": f"generation-{arm_id}-1",
        }
        item["cell_id"] = gateway_probe_results.cell_id(item["identity"])
    (run_dir / "prices.json").write_text(_json(prices), encoding="ascii")
    (run_dir / "results.jsonl").write_text(
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in rows
        ),
        encoding="ascii",
    )
    report = gateway_probe_report.aggregate(rows, experiment=experiment)
    (run_dir / "report.json").write_text(_json(report), encoding="ascii")
    (run_dir / "report.md").write_text(
        gateway_probe_report.render_text(report) + "\n",
        encoding="utf-8",
    )
    names = (
        "experiment.toml",
        "prices.json",
        "results.jsonl",
        "report.json",
        "report.md",
    )
    source_manifest = {
        "schema_version": 1,
        "benchmark": "gateway_probe",
        "result_schema_version": 3,
        "experiment_id": experiment.experiment_id,
        "experiment_digest": experiment.digest,
        "files": {
            name: {"sha256": _sha256(run_dir / name)}
            for name in names
        },
    }
    (run_dir / "manifest.json").write_text(
        _json(source_manifest),
        encoding="ascii",
    )
    return run_dir


class GatewayProbePublishP0SecurityTests(unittest.TestCase):
    def test_detected_verifier_commit_rejects_dirty_verifier_source(self):
        dirty = CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="?? obench/gateway_probe_publish.py\n",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=dirty) as run:
            with self.assertRaisesRegex(GatewayProbeRunError, "source is dirty"):
                gateway_probe_publish._detect_verifier_commit()
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertTrue(set(
            gateway_probe_publish.VERIFIER_SOURCE_FILES
        ).issubset(command))
        self.assertNotIn("obench/site.py", command)

    def test_migrates_legacy_source_report_without_publishing_its_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            report_path = run_dir / "report.json"
            report = json.loads(report_path.read_text())
            report["schema_version"] = 3
            report["label"] = "retired-private-label"
            report_path.write_text(_json(report), encoding="ascii")
            markdown_path = run_dir / "report.md"
            lines = markdown_path.read_text().splitlines()
            lines[0] = "Gateway Probe (retired-private-label)"
            markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            source_manifest = json.loads(
                (run_dir / "manifest.json").read_text()
            )
            for name in ("report.json", "report.md"):
                source_manifest["files"][name]["sha256"] = _sha256(
                    run_dir / name
                )
            (run_dir / "manifest.json").write_text(
                _json(source_manifest),
                encoding="ascii",
            )

            bundle = Path(tmp, "public")
            gateway_probe_publish.publish_bundle(
                run_dir,
                bundle,
                verified_with_commit=TEST_COMMIT,
            )
            public_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in bundle.iterdir()
            )
            self.assertNotIn("retired-private-label", public_text)
            self.assertEqual(
                json.loads((bundle / "report.json").read_text())[
                    "schema_version"
                ],
                4,
            )

    def test_publish_projects_only_public_dto_and_removes_operational_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            bundle = Path(tmp, "public")
            published = gateway_probe_publish.publish_bundle(
                run_dir,
                bundle,
                verified_with_commit=TEST_COMMIT,
            )
            verified = gateway_probe_publish.verify_bundle(bundle)

            self.assertEqual(verified, published)
            self.assertEqual(
                {path.name for path in bundle.iterdir()},
                {
                    "prices.json",
                    "schedule.json",
                    "results.jsonl",
                    "report.json",
                    "report.md",
                    "manifest.json",
                },
            )
            self.assertEqual(published["report_schema_version"], 4)
            self.assertEqual(published["complete_blocks"], {"cold": 2, "warm": 2})
            self.assertEqual(published["scheduled_blocks_per_condition"], 2)
            self.assertNotIn("label", published)
            self.assertEqual(
                published["run_provenance"],
                {
                    "source_commit": "unknown",
                    "started_at": "unknown",
                    "completed_at": "unknown",
                },
            )
            public_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in bundle.iterdir()
            )
            self.assertNotIn("PRIVATE PROMPT", public_text)
            self.assertNotIn("api.openai.com", public_text)
            self.assertNotIn("OPENAI_API_KEY", public_text)
            self.assertNotIn("generation-direct", public_text)
            self.assertNotIn("receipt-direct", public_text)
            self.assertNotIn("exploratory", public_text)
            self.assertNotIn("confirmatory", public_text)
            rows = gateway_probe_results.load_results(bundle / "results.jsonl")
            self.assertTrue(all(
                not item["request_metrics"]["receipt_headers"]
                and not item["reuse_evidence"]["receipt_headers"]
                for item in rows
            ))

    def test_rejects_secret_path_account_and_invalid_verifier_commit(self):
        unsafe = (
            {"value": "sk-private-123456789"},
            {"value": "/Users/private/account.json"},
            {"value": "0123456789abcdef0123456789abcdef"},
            {"account_id": "tenant"},
        )
        for value in unsafe:
            with self.subTest(value=value):
                with self.assertRaises(GatewayProbeRunError):
                    gateway_probe_publish._assert_public_safe(value)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            with self.assertRaisesRegex(GatewayProbeRunError, "git commit"):
                gateway_probe_publish.publish_bundle(
                    run_dir,
                    Path(tmp, "public"),
                    verified_with_commit="short",
                )
            with self.assertRaisesRegex(
                GatewayProbeRunError,
                "must resolve to a git commit",
            ):
                gateway_probe_publish.publish_bundle(
                    run_dir,
                    Path(tmp, "missing-commit"),
                    verified_with_commit="d" * 40,
                )

    def test_rejects_source_digest_drift_extra_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            (run_dir / "extra.log").write_text("private", encoding="utf-8")
            with self.assertRaisesRegex(GatewayProbeRunError, "file set is not exact"):
                gateway_probe_publish.publish_bundle(
                    run_dir,
                    Path(tmp, "public"),
                    verified_with_commit=TEST_COMMIT,
                )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            (run_dir / "report.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(GatewayProbeRunError, "digest mismatch"):
                gateway_probe_publish.publish_bundle(
                    run_dir,
                    Path(tmp, "public"),
                    verified_with_commit=TEST_COMMIT,
                )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            prices = run_dir / "prices.json"
            target = run_dir / "prices-target.json"
            prices.rename(target)
            prices.symlink_to(target)
            with self.assertRaisesRegex(GatewayProbeRunError, "file set is not exact"):
                gateway_probe_publish.publish_bundle(
                    run_dir,
                    Path(tmp, "public"),
                    verified_with_commit=TEST_COMMIT,
                )


class GatewayProbePublishP1IntegrityTests(unittest.TestCase):
    def _bundle(self, tmp):
        run_dir = build_private_run(tmp)
        bundle = Path(tmp, "public")
        gateway_probe_publish.publish_bundle(
            run_dir,
            bundle,
            verified_with_commit=TEST_COMMIT,
        )
        return bundle

    def test_rejects_rehashed_results_tamper_via_report_recomputation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            lines = (bundle / "results.jsonl").read_text().splitlines()
            changed = json.loads(lines[0])
            changed["request_metrics"]["costs"]["frozen_list_estimate"][
                "amount_usd"
            ] = 0.002
            changed["billing"]["measured_cost_usd"] = "0.002"
            changed["billing"]["charged_cost_usd"] = "0.002"
            lines[0] = json.dumps(changed, separators=(",", ":"), sort_keys=True)
            (bundle / "results.jsonl").write_text(
                "\n".join(lines) + "\n",
                encoding="ascii",
            )
            _rewrite_manifest_hash(bundle, "results.jsonl")
            with self.assertRaisesRegex(GatewayProbeRunError, "recomputed report"):
                gateway_probe_publish.verify_bundle(bundle)

    def test_rejects_rehashed_schedule_coordinate_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            schedule_path = bundle / "schedule.json"
            schedule = json.loads(schedule_path.read_text())
            schedule["blocks"][0]["repetition"] = 99
            schedule_path.write_text(_json(schedule), encoding="ascii")
            _rewrite_manifest_hash(bundle, "schedule.json")
            with self.assertRaisesRegex(
                GatewayProbeRunError,
                "schedule does not match results",
            ):
                gateway_probe_publish.verify_bundle(bundle)

    def test_rejects_rehashed_report_tamper_and_qualitative_label_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            report_path = bundle / "report.json"
            report = json.loads(report_path.read_text())
            report["complete_blocks"]["cold"] = 99
            report["label"] = "confirmatory"
            report_path.write_text(_json(report), encoding="ascii")
            _rewrite_manifest_hash(bundle, "report.json")
            with self.assertRaisesRegex(GatewayProbeRunError, "recomputed report"):
                gateway_probe_publish.verify_bundle(bundle)

    def test_rejects_unsafe_rehashed_public_dto_and_extra_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            results_path = bundle / "results.jsonl"
            lines = results_path.read_text().splitlines()
            changed = json.loads(lines[0])
            changed["request_metrics"]["route"][
                "served_model"
            ] = "/Users/private/route"
            lines[0] = json.dumps(
                changed, separators=(",", ":"), sort_keys=True
            )
            results_path.write_text("\n".join(lines) + "\n", encoding="ascii")
            _rewrite_manifest_hash(bundle, "results.jsonl")
            with self.assertRaisesRegex(GatewayProbeRunError, "URL or path"):
                gateway_probe_publish.verify_bundle(bundle)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            (bundle / "notes.txt").write_text("not allowed", encoding="utf-8")
            with self.assertRaisesRegex(GatewayProbeRunError, "file set is not exact"):
                gateway_probe_publish.verify_bundle(bundle)

    def test_rejects_manifest_provenance_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            path = bundle / "manifest.json"
            manifest_value = json.loads(path.read_text())
            manifest_value["run_provenance"]["source_commit"] = TEST_COMMIT
            path.write_text(_json(manifest_value), encoding="ascii")
            with self.assertRaisesRegex(GatewayProbeRunError, "explicitly unknown"):
                gateway_probe_publish.verify_bundle(bundle)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp)
            path = bundle / "manifest.json"
            manifest_value = json.loads(path.read_text())
            manifest_value["verification"]["verified_with_commit"] = "d" * 40
            path.write_text(_json(manifest_value), encoding="ascii")
            with self.assertRaisesRegex(
                GatewayProbeRunError,
                "must resolve to a git commit",
            ):
                gateway_probe_publish.verify_bundle(bundle)

    def test_rejects_incomplete_cold_or_warm_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = build_private_run(tmp)
            experiment = gateway_probe_spec.load_experiment(
                run_dir / "experiment.toml"
            )
            rows = gateway_probe_results.load_results(run_dir / "results.jsonl")
            rows = [
                item
                for item in rows
                if item["identity"]["schedule"]["repetition"] == 1
            ]
            (run_dir / "results.jsonl").write_text(
                "".join(
                    json.dumps(item, separators=(",", ":"), sort_keys=True)
                    + "\n"
                    for item in rows
                ),
                encoding="ascii",
            )
            report = gateway_probe_report.aggregate(rows, experiment=experiment)
            (run_dir / "report.json").write_text(_json(report), encoding="ascii")
            (run_dir / "report.md").write_text(
                gateway_probe_report.render_text(report) + "\n",
                encoding="utf-8",
            )
            source_manifest = json.loads(
                (run_dir / "manifest.json").read_text()
            )
            for name in ("results.jsonl", "report.json", "report.md"):
                source_manifest["files"][name]["sha256"] = _sha256(
                    run_dir / name
                )
            (run_dir / "manifest.json").write_text(
                _json(source_manifest),
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                GatewayProbeRunError,
                "requires every scheduled cold and warm block",
            ):
                gateway_probe_publish.publish_bundle(
                    run_dir,
                    Path(tmp, "public"),
                    verified_with_commit=TEST_COMMIT,
                )


if __name__ == "__main__":
    unittest.main()
