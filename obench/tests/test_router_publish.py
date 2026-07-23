"""Tests for sanitized Router Bench publishing and verification."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from obench import results
from obench import router_publish
from obench import router_report
from obench import router_spec


def digest(label):
    return results.canonical_digest({"fixture": label})


def canonical_line(value):
    return results.canonical_json(value) + "\n"


def experiment():
    return router_spec.parse_experiment({
        "schema_version": 1,
        "experiment_id": "gateway-tax-publish",
        "track": "gateway_tax",
        "harness": "pi",
        "tasks": ["make-it-run"],
        "repetitions_per_window": 1,
        "schedule_seed": 17,
        "execution_lane": "docker",
        "private_router": False,
        "windows": [{
            "window_id": "morning",
            "start": "2026-07-22T08:00:00Z",
            "end": "2026-07-22T09:00:00Z",
        }],
        "budget": {
            "timeout_s": 300,
            "max_calls": 8,
            "max_output_tokens": 16000,
            "usd_cap": "2.5",
        },
        "arms": [
            {
                "arm_id": "direct",
                "route_kind": "direct",
                "endpoint": "https://direct.example.test/v1/chat/completions",
                "protocol": "openai_chat",
                "baseline": True,
                "canonical_model": "openai/gpt-test",
                "requested_model": "openai/gpt-test",
                "requested_provider": "OpenAI",
                "allowed_models": ["openai/gpt-test"],
                "allowed_providers": ["OpenAI"],
                "fallback_enabled": False,
                "retry_count": 0,
                "cache_enabled": False,
                "auth_env": "VERY_PRIVATE_DIRECT_KEY",
                "sampling": {"temperature": 0.0, "top_p": 1.0, "seed": 17},
            },
            {
                "arm_id": "gateway",
                "route_kind": "gateway",
                "gateway": "openrouter",
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                "protocol": "openai_chat",
                "baseline": False,
                "canonical_model": "openai/gpt-test",
                "requested_model": "openai/gpt-test",
                "requested_provider": "OpenAI",
                "allowed_models": ["openai/gpt-test"],
                "allowed_providers": ["OpenAI"],
                "fallback_enabled": False,
                "retry_count": 0,
                "cache_enabled": False,
                "auth_env": "VERY_PRIVATE_ROUTER_KEY",
                "sampling": {"temperature": 0.0, "top_p": 1.0, "seed": 17},
                "direct_control_arm_id": "direct",
            },
        ],
    })


class RouterPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="router_publish_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.results_path = self.root / "source-results.jsonl"
        self.source_ledger = self.root / "private-ledger.jsonl"
        self.bundle = self.root / "bundle"
        self.experiment = experiment()
        self.policy = {
            "schema_version": 1,
            "policy_id": "strict",
            "allowed_models": ["openai/gpt-test"],
            "allowed_providers": ["OpenAI"],
            "fallback_enabled": False,
            "retry_count": 0,
            "raw_headers": {"Authorization": "Bearer should-never-publish"},
        }
        self.catalog = {
            "schema_version": 1,
            "catalog_id": "catalog-1",
            "models": [{
                "model": "openai/gpt-test",
                "provider": "OpenAI",
                "context_window": 128000,
                "endpoint": "https://router.example.test/private/model",
            }],
        }
        self.prices = {
            "schema_version": 1,
            "price_id": "prices-1",
            "currency": "USD",
            "prices": [{
                "model": "openai/gpt-test",
                "provider": "OpenAI",
                "input_per_million": "1.25",
                "output_per_million": "5.00",
                "account_id": "123456789012",
            }],
        }
        self.identity = results.CellIdentity.for_router(
            track="gateway_tax",
            experiment_id=self.experiment.experiment_id,
            experiment_digest=self.experiment.digest,
            arm_id="gateway",
            arm_digest=self.experiment.arms[1].digest,
            policy_digest=results.canonical_digest(self.policy),
            catalog_digest=results.canonical_digest(self.catalog),
            price_digest=results.canonical_digest(self.prices),
            sampling_digest=digest("sampling"),
            schedule_digest=digest("schedule"),
            task="make-it-run",
            task_digest=digest("task"),
            checker_digest=digest("checker"),
            workspace_source_sha="a" * 40,
            harness="pi",
            candidate=None,
            harness_version="0.80.10",
            execution_lane="docker",
            image_digest=digest("image"),
            budget_timeout_s=300,
            budget_max_calls=8,
            budget_max_output_tokens=16000,
            budget_usd_cap="2.5",
            adapter_timeout_s=240,
            checker_timeout_s=60,
            window_id="morning",
            repetition=1,
            block_id="block-001",
            block_attempt=0,
        )
        self.cell_id = results.make_router_cell_id(self.identity)
        self.row = {
            "schema_version": 2,
            "benchmark": "router",
            "identity": self.identity.as_dict(),
            "run_id": results.make_router_run_id(self.identity),
            "cell_id": self.cell_id,
            "expected_arm_ids": ["direct", "gateway"],
            "arm_role": "gateway",
            "baseline": False,
            "result": {
                "solved": True,
                "checker_score": 1.0,
                "available": True,
                "duration_s": 12.0,
                "timed_out": False,
                "infrastructure_invalid_reason": None,
            },
            "route_integrity": {"pass": True, "reasons": []},
            "route_isolation": {
                "classification": "exploratory",
                "lane": "router-local-v1",
                "egress_enforced": False,
            },
            "proxy_metrics": {"calls": [{
                "timing": {"ttfb_s": 1.0, "semantic_ttft_s": 2.0},
                "generation": {"output_tokens": 4, "duration_s": 1.0},
                "route": {
                    "provider": "OpenAI",
                    "served_model": "openai/gpt-test",
                },
                "costs": {
                    "frozen_list_estimate": {
                        "amount_usd": 0.001,
                        "currency": "USD",
                        "effective_at": "2026-07-01T00:00:00Z",
                    },
                },
            }]},
            "raw_headers": {"authorization": "Bearer source-result-secret"},
            "query": "api_key=source-result-secret",
            "request_body": "private prompt",
            "response_body": "private answer",
            "transcript_path": "/Users/private/transcript.txt",
        }
        self._write_ledger()

    def _write_results(self, *rows):
        self.results_path.write_text(
            "".join(canonical_line(row) for row in rows),
            encoding="utf-8",
        )

    def _write_ledger(self, *, allowed_secret=None, partial=False):
        request = {
            "record_type": "request",
            "sequence": 1,
            "previous_hash": hashlib.sha256(b"").hexdigest(),
            "ts": "2026-07-22T08:01:00Z",
            "method": "POST",
            "path": "/cell/private-token/chat/router/private/v1/chat/completions",
            "upstream": "https://router.example.test/private/v1",
            "status": 200,
            "model": allowed_secret or "openai/gpt-test",
            "usage": (
                None
                if partial
                else {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}
            ),
            "router_arm": {
                "arm_id": "gateway",
                "arm_digest": self.identity.arm_digest,
                "route_kind": "gateway",
            },
            "error": "request body and credentials must never publish",
        }
        if partial:
            request["router_metrics"] = None
        request["record_hash"] = hashlib.sha256(
            results.canonical_json_bytes({
                key: value for key, value in request.items() if key != "record_hash"
            })
        ).hexdigest()
        seal = {
            "record_type": "ledger_seal",
            "state": "SEALED",
            "record_count": 1,
            "last_sequence": 1,
            "root_hash": request["record_hash"],
        }
        self.source_ledger.write_text(
            canonical_line(request) + canonical_line(seal),
            encoding="utf-8",
        )
        self.row["ledger_seal"] = {
            "record_count": seal["record_count"],
            "last_sequence": seal["last_sequence"],
            "root_hash": seal["root_hash"],
            "ledger_file": self.source_ledger.name,
        }
        self._write_results(self.row)

    def publish(self):
        return router_publish.publish_bundle(
            self.results_path,
            self.bundle,
            experiment=self.experiment,
            policy=self.policy,
            catalog=self.catalog,
            prices=self.prices,
            ledgers={self.cell_id: self.source_ledger},
        )

    def test_round_trip_uses_allowlisted_dtos_and_binds_all_artifacts(self):
        provenance = self.publish()
        self.assertEqual(router_publish.verify_bundle(self.bundle), provenance)

        files = {
            path.relative_to(self.bundle).as_posix()
            for path in self.bundle.rglob("*")
            if path.is_file()
        }
        self.assertEqual(files, set(provenance["artifacts"]) | {"provenance.json"})
        for relative, expected in provenance["artifacts"].items():
            self.assertEqual(
                hashlib.sha256((self.bundle / relative).read_bytes()).hexdigest(),
                expected,
            )

        bundle_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.bundle.rglob("*")
            if path.is_file()
        )
        for forbidden in (
            "raw_headers", "source-result-secret", "private prompt",
            "private answer", "transcript_path", "/Users/private",
            "VERY_PRIVATE_ROUTER_KEY", "/private/v1", "account_id",
            "123456789012", "credentials must never publish",
        ):
            self.assertNotIn(forbidden, bundle_text)

        public_row = json.loads((self.bundle / "results.jsonl").read_text())
        binding = public_row["ledger"]
        self.assertEqual(binding, provenance["ledgers"][self.cell_id])
        seal = json.loads(
            (self.bundle / binding["artifact"]).read_text().splitlines()[-1]
        )
        self.assertEqual(seal["cell_id"], self.cell_id)
        self.assertEqual(seal["root_hash"], binding["root_hash"])
        self.assertEqual(seal["seal_sha256"], binding["seal_sha256"])

    def test_published_results_round_trip_through_router_report(self):
        direct_identity = dataclasses.replace(
            self.identity,
            arm_id="direct",
            arm_digest=self.experiment.arms[0].digest,
        )
        direct = copy.deepcopy(self.row)
        direct["identity"] = direct_identity.as_dict()
        direct["run_id"] = results.make_router_run_id(direct_identity)
        direct["cell_id"] = results.make_router_cell_id(direct_identity)
        direct["arm_role"] = "direct"
        direct["baseline"] = True
        direct["result"]["duration_s"] = 10.0
        direct["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 0.5
        direct["proxy_metrics"]["calls"][0]["timing"]["semantic_ttft_s"] = 1.0
        direct["proxy_metrics"]["calls"][0]["route"]["provider"] = "Direct"
        direct_ledger = self.root / "direct-ledger.jsonl"
        request = json.loads(self.source_ledger.read_text().splitlines()[0])
        request["router_arm"] = {
            "arm_id": "direct",
            "arm_digest": direct_identity.arm_digest,
            "route_kind": "direct",
        }
        request["record_hash"] = hashlib.sha256(
            results.canonical_json_bytes({
                key: value for key, value in request.items() if key != "record_hash"
            })
        ).hexdigest()
        seal = {
            "record_type": "ledger_seal",
            "state": "SEALED",
            "record_count": 1,
            "last_sequence": 1,
            "root_hash": request["record_hash"],
        }
        direct_ledger.write_text(
            canonical_line(request) + canonical_line(seal), encoding="utf-8"
        )
        direct["ledger_seal"] = {
            "record_count": 1,
            "last_sequence": 1,
            "root_hash": request["record_hash"],
            "ledger_file": direct_ledger.name,
        }

        source_rows = [direct, self.row]
        self._write_results(*source_rows)
        router_publish.publish_bundle(
            self.results_path,
            self.bundle,
            experiment=self.experiment,
            policy=self.policy,
            catalog=self.catalog,
            prices=self.prices,
            ledgers={
                direct["cell_id"]: direct_ledger,
                self.cell_id: self.source_ledger,
            },
        )
        public_rows = [
            json.loads(line)
            for line in (self.bundle / "results.jsonl").read_text().splitlines()
        ]

        expected = router_report.aggregate(
            source_rows, bootstrap_replicates=20, bootstrap_seed=7
        )
        actual = router_report.aggregate(
            public_rows, bootstrap_replicates=20, bootstrap_seed=7
        )
        self.assertEqual(actual, expected)

    def test_partial_failed_call_with_nullable_evidence_is_published(self):
        call = self.row["proxy_metrics"]["calls"][0]
        call["timing"] = None
        call["generation"] = None
        call["route"] = None
        call["costs"] = None
        self.row["result"].update(
            solved=False,
            checker_score=0.0,
            available=False,
            duration_s=None,
            timed_out=True,
        )
        self._write_results(self.row)
        self._write_ledger(partial=True)

        provenance = self.publish()
        self.assertEqual(router_publish.verify_bundle(self.bundle), provenance)
        public_row = json.loads((self.bundle / "results.jsonl").read_text())
        public_call = public_row["proxy_metrics"]["calls"][0]
        self.assertNotIn("timing", public_call)
        self.assertIsNone(public_call["generation"])
        self.assertNotIn("route", public_call)
        self.assertIsNone(public_call["costs"])
        public_ledger = self.bundle / public_row["ledger"]["artifact"]
        request = json.loads(public_ledger.read_text().splitlines()[0])
        self.assertIsNone(request["usage"])
        self.assertIsNone(request["router_metrics"])

    def test_partial_call_shape_validation_remains_fail_closed(self):
        self.row["proxy_metrics"]["calls"][0]["generation"] = "unknown"
        self._write_results(self.row)

        with self.assertRaisesRegex(
            router_publish.RouterPublishError,
            "generation must be an object",
        ):
            self.publish()

    def test_publish_rejects_corrupt_jsonl_and_cell_ledger_mismatch(self):
        self.results_path.write_bytes(self.results_path.read_bytes()[:-1])
        with self.assertRaisesRegex(router_publish.RouterPublishError, "newline"):
            self.publish()

        self._write_results(self.row)
        with self.assertRaisesRegex(router_publish.RouterPublishError, "bindings mismatch"):
            router_publish.publish_bundle(
                self.results_path,
                self.bundle,
                experiment=self.experiment,
                policy=self.policy,
                catalog=self.catalog,
                prices=self.prices,
                ledgers={"router-cell-v2-wrong": self.source_ledger},
            )

    def test_publish_rejects_unsealed_or_tampered_source_ledger(self):
        lines = self.source_ledger.read_text().splitlines()
        self.source_ledger.write_text(lines[0] + "\n", encoding="utf-8")
        with self.assertRaisesRegex(router_publish.RouterPublishError, "terminal seal"):
            self.publish()

        self._write_ledger()
        request, seal = [
            json.loads(line) for line in self.source_ledger.read_text().splitlines()
        ]
        request["status"] = 500
        self.source_ledger.write_text(
            canonical_line(request) + canonical_line(seal),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(router_publish.RouterPublishError, "tampered"):
            self.publish()

    def test_publish_rejects_source_ledger_not_bound_to_result(self):
        self.row["ledger_seal"]["root_hash"] = "0" * 64
        self._write_results(self.row)
        with self.assertRaisesRegex(router_publish.RouterPublishError, "does not match"):
            self.publish()

    def test_verify_rejects_tamper_extra_artifact_and_binding_mismatch(self):
        self.publish()
        results_path = self.bundle / "results.jsonl"
        results_path.write_bytes(
            results_path.read_bytes().replace(
                b'"checker_score":1.0',
                b'"checker_score":0.0',
            )
        )
        with self.assertRaisesRegex(router_publish.RouterPublishError, "digest mismatch"):
            router_publish.verify_bundle(self.bundle)

        results_path.write_text(canonical_line(
            router_publish._decode_json(  # noqa: SLF001 - restore test fixture
                self.results_path.read_bytes().splitlines()[0], "fixture"
            )
        ), encoding="utf-8")
        # Re-publish is simpler than reconstructing the public row and digest.
        for child in self.bundle.rglob("*"):
            if child.is_file():
                child.unlink()
        for child in sorted(self.bundle.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
        self.bundle.rmdir()
        self.publish()
        (self.bundle / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(router_publish.RouterPublishError, "artifact set mismatch"):
            router_publish.verify_bundle(self.bundle)

        (self.bundle / "unexpected.txt").unlink()
        price_path = self.bundle / "prices.json"
        price_raw = price_path.read_bytes()
        price_path.unlink()
        with self.assertRaisesRegex(router_publish.RouterPublishError, "artifact set mismatch"):
            router_publish.verify_bundle(self.bundle)
        price_path.write_bytes(price_raw)

        public_rows = [
            json.loads(line)
            for line in (self.bundle / "results.jsonl").read_text().splitlines()
        ]
        public_rows[0]["ledger"]["root_hash"] = "0" * 64
        raw = "".join(canonical_line(row) for row in public_rows).encode()
        (self.bundle / "results.jsonl").write_bytes(raw)
        provenance_path = self.bundle / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["artifacts"]["results.jsonl"] = hashlib.sha256(raw).hexdigest()
        provenance_path.write_text(canonical_line(provenance), encoding="utf-8")
        with self.assertRaisesRegex(router_publish.RouterPublishError, "ledger binding mismatch"):
            router_publish.verify_bundle(self.bundle)

    def test_verify_rejects_corrupt_jsonl_even_when_artifact_hash_is_updated(self):
        self.publish()
        path = self.bundle / "results.jsonl"
        raw = path.read_bytes()[:-1]
        path.write_bytes(raw)
        provenance_path = self.bundle / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["artifacts"]["results.jsonl"] = hashlib.sha256(raw).hexdigest()
        provenance_path.write_text(canonical_line(provenance), encoding="utf-8")
        with self.assertRaisesRegex(router_publish.RouterPublishError, "newline"):
            router_publish.verify_bundle(self.bundle)

    def test_secret_in_allowlisted_value_is_rejected_on_publish_and_verify(self):
        self._write_ledger(allowed_secret="sk-abcdefghijklmnopqrstuvwxyz")
        with self.assertRaisesRegex(router_publish.RouterPublishError, "credential pattern"):
            self.publish()

        self._write_ledger()
        self.publish()
        catalog_path = self.bundle / "catalog.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["data"]["name"] = "leak@example.com"
        raw = canonical_line(catalog).encode()
        catalog_path.write_bytes(raw)
        provenance_path = self.bundle / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["artifacts"]["catalog.json"] = hashlib.sha256(raw).hexdigest()
        provenance_path.write_text(canonical_line(provenance), encoding="utf-8")
        with self.assertRaisesRegex(router_publish.RouterPublishError, "email"):
            router_publish.verify_bundle(self.bundle)

    def test_snapshot_digest_mismatch_fails_closed(self):
        wrong = dataclasses.replace(self.identity, policy_digest=digest("wrong-policy"))
        row = dict(self.row)
        row["identity"] = wrong.as_dict()
        row["run_id"] = results.make_router_run_id(wrong)
        row["cell_id"] = results.make_router_cell_id(wrong)
        self._write_results(row)
        with self.assertRaisesRegex(router_publish.RouterPublishError, "snapshot digests"):
            router_publish.publish_bundle(
                self.results_path,
                self.bundle,
                experiment=self.experiment,
                policy=self.policy,
                catalog=self.catalog,
                prices=self.prices,
                ledgers={row["cell_id"]: self.source_ledger},
            )


if __name__ == "__main__":
    unittest.main()
