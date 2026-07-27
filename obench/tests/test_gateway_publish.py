"""Tests for sanitized Gateway Bench publishing and verification."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from obench import results
from obench import gateway_publish
from obench import gateway_report
from obench import gateway_spec


def digest(label):
    return results.canonical_digest({"fixture": label})


def canonical_line(value):
    return results.canonical_json(value) + "\n"


def experiment():
    return gateway_spec.parse_experiment({
        "schema_version": 2,
        "experiment_id": "gateway-bench-publish",
        "track": "fixed_model_provider",
        "provider_prompt_mode": "provider_default",
        "harness": "pi",
        "tasks": ["make-it-run"],
        "repetitions_per_window": 1,
        "schedule_seed": 17,
        "execution_lane": "docker",
        "allow_private_endpoint": False,
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
                "gateway": "vercel",
                "endpoint": "https://ai-gateway.vercel.sh/v1/chat/completions",
                "protocol": "openai_chat",
                "baseline": False,
                "canonical_model": "openai/gpt-test",
                "requested_model": "openai/gpt-test",
                "requested_provider": "OpenAI",
                "allowed_models": [
                    "openai/gpt-test",
                    "gpt-test-2026-07-22",
                ],
                "allowed_providers": ["OpenAI"],
                "fallback_enabled": False,
                "retry_count": 0,
                "cache_enabled": False,
                "auth_env": "VERY_PRIVATE_GATEWAY_KEY",
                "sampling": {"temperature": 0.0, "top_p": 1.0, "seed": 17},
                "direct_control_arm_id": "direct",
            },
        ],
    })


class GatewayPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway_publish_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.results_path = self.root / "source-results.jsonl"
        self.source_ledger = self.root / "private-ledger.jsonl"
        self.bundle = self.root / "bundle"
        self.experiment = experiment()
        self.policy = {
            "schema_version": 1,
            "policy_id": "strict",
            "provider_prompt_mode": "provider_default",
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
        self.identity = results.CellIdentity.for_gateway(
            track="fixed_model_provider",
            experiment_id=self.experiment.experiment_id,
            experiment_digest=self.experiment.digest,
            arm_id="gateway",
            arm_digest=self.experiment.arms[1].digest,
            policy_digest=results.canonical_digest(self.policy),
            catalog_digest=results.canonical_digest(self.catalog),
            price_digest=results.canonical_digest(self.prices),
            sampling_digest=digest("sampling"),
        schedule_digest=digest("schedule"),
        provider_prompt_mode="provider_default",
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
        self.cell_id = results.make_gateway_cell_id(self.identity)
        self.row = {
            "schema_version": 2,
            "benchmark": "gateway",
            "identity": self.identity.as_dict(),
            "run_id": results.make_gateway_run_id(self.identity),
            "cell_id": self.cell_id,
            "expected_arm_ids": ["direct", "gateway"],
            "arm_role": "gateway",
            "baseline": False,
            "provider_prompt_mode": "provider_default",
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
                "lane": "gateway-local-v1",
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

    def test_public_experiment_retains_cloudflare_managed_gateway_id(self):
        source = experiment().to_dict()
        gateway = source["arms"][1]
        gateway.update({
            "gateway": "cloudflare",
            "gateway_id": "openbench-gateway-bench",
            "endpoint": (
                "https://api.cloudflare.com/client/v4/accounts/"
                "0123456789abcdef0123456789abcdef/ai/v1/chat/completions"
            ),
            "auth_env": "CLOUDFLARE_API_TOKEN",
        })
        managed = gateway_spec.parse_experiment(source)

        public = gateway_publish._experiment_dto(
            managed.to_dict(),
            managed.digest,
        )

        self.assertEqual(
            public["arms"][1]["gateway_id"],
            "openbench-gateway-bench",
        )
        self.assertEqual(
            public["arms"][1]["arm_digest"],
            managed.arms[1].digest,
        )

    def test_provider_prompt_mode_is_bound_across_publication_artifacts(self):
        gateway_publish._require_provider_prompt_mode_binding(  # noqa: SLF001
            self.experiment.to_dict(),
            self.policy,
            [self.row],
        )

        wrong_policy = dict(self.policy, provider_prompt_mode="isolated_per_call_v1")
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "policy provider_prompt_mode",
        ):
            gateway_publish._require_provider_prompt_mode_binding(  # noqa: SLF001
                self.experiment.to_dict(),
                wrong_policy,
                [self.row],
            )

        wrong_row = dict(self.row, provider_prompt_mode="isolated_per_call_v1")
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "result .* provider_prompt_mode",
        ):
            gateway_publish._require_provider_prompt_mode_binding(  # noqa: SLF001
                self.experiment.to_dict(),
                self.policy,
                [wrong_row],
            )

    def _write_results(self, *rows):
        self.results_path.write_text(
            "".join(canonical_line(row) for row in rows),
            encoding="utf-8",
        )

    def _write_ledger(
        self,
        *,
        allowed_secret=None,
        partial=False,
        gateway_metrics=None,
    ):
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
            "serving_arm": {
                "arm_id": "gateway",
                "arm_digest": self.identity.arm_digest,
                "route_kind": "gateway",
            },
            "provider_cache": {
                "mode": "provider_default",
                "transform_id": None,
                "prefix_injected": False,
                "scope": None,
                "nonce_commitment": None,
            },
            "forwarded_upstream": True,
            "error": "request body and credentials must never publish",
        }
        if partial:
            request["gateway_metrics"] = None
        elif gateway_metrics is not None:
            request["gateway_metrics"] = gateway_metrics
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
        ledgers = {self.cell_id: self.source_ledger}
        raw_results = self.results_path.read_bytes()
        try:
            source_rows = [
                json.loads(line)
                for line in self.results_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        except (OSError, json.JSONDecodeError):
            source_rows = []
        if hasattr(self, "_direct_cell_id") and any(
            row.get("cell_id") == self._direct_cell_id for row in source_rows
        ):
            ledgers[self._direct_cell_id] = self._direct_ledger
        if (
            raw_results.endswith(b"\n")
            and len(source_rows) == 1
            and source_rows[0].get("cell_id") == self.cell_id
        ):
            direct_identity = dataclasses.replace(
                self.identity,
                arm_id="direct",
                arm_digest=self.experiment.arms[0].digest,
            )
            direct = copy.deepcopy(source_rows[0])
            direct["identity"] = direct_identity.as_dict()
            direct["run_id"] = results.make_gateway_run_id(direct_identity)
            direct["cell_id"] = results.make_gateway_cell_id(direct_identity)
            direct["arm_role"] = "direct"
            direct["baseline"] = True

            direct_ledger = self.root / "direct-companion-ledger.jsonl"
            source_ledger_rows = [
                json.loads(line)
                for line in self.source_ledger.read_text(encoding="utf-8").splitlines()
                if line
            ]
            requests = source_ledger_rows[:-1]
            previous = hashlib.sha256(b"").hexdigest()
            rewritten = []
            for sequence, request in enumerate(requests, 1):
                request = copy.deepcopy(request)
                request["sequence"] = sequence
                request["previous_hash"] = previous
                request["serving_arm"] = {
                    "arm_id": "direct",
                    "arm_digest": direct_identity.arm_digest,
                    "route_kind": "direct",
                }
                request["record_hash"] = hashlib.sha256(
                    results.canonical_json_bytes({
                        key: value
                        for key, value in request.items()
                        if key != "record_hash"
                    })
                ).hexdigest()
                previous = request["record_hash"]
                rewritten.append(request)
            seal = {
                "record_type": "ledger_seal",
                "state": "SEALED",
                "record_count": len(rewritten),
                "last_sequence": len(rewritten),
                "root_hash": previous,
            }
            direct_ledger.write_text(
                "".join(canonical_line(item) for item in [*rewritten, seal]),
                encoding="utf-8",
            )
            direct["ledger_seal"] = {
                "record_count": len(rewritten),
                "last_sequence": len(rewritten),
                "root_hash": previous,
                "ledger_file": direct_ledger.name,
            }
            self._write_results(direct, source_rows[0])
            ledgers[direct["cell_id"]] = direct_ledger
            self._direct_cell_id = direct["cell_id"]
            self._direct_ledger = direct_ledger
        return gateway_publish.publish_bundle(
            self.results_path,
            self.bundle,
            experiment=self.experiment,
            policy=self.policy,
            catalog=self.catalog,
            prices=self.prices,
            ledgers=ledgers,
        )

    def _public_gateway_row(self):
        rows = [
            json.loads(line)
            for line in (self.bundle / "results.jsonl").read_text().splitlines()
        ]
        return next(
            row for row in rows
            if row["identity"]["arm"]["id"] == "gateway"
        )

    def _rewrite_public_gateway_row(self, update):
        results_path = self.bundle / "results.jsonl"
        rows = [
            json.loads(line)
            for line in results_path.read_text(encoding="utf-8").splitlines()
        ]
        gateway_row = next(
            row for row in rows
            if row["identity"]["arm"]["id"] == "gateway"
        )
        update(gateway_row)
        raw = "".join(canonical_line(row) for row in rows).encode()
        results_path.write_bytes(raw)

        provenance_path = self.bundle / "provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["artifacts"]["results.jsonl"] = hashlib.sha256(raw).hexdigest()
        provenance_path.write_text(canonical_line(provenance), encoding="utf-8")

    def test_round_trip_uses_allowlisted_dtos_and_binds_all_artifacts(self):
        self.row["proxy_metrics"]["calls"][0]["cache"] = {
            "cached_input_tokens": 7,
            "cache_write_input_tokens": 3,
        }
        self._write_results(self.row)
        provenance = self.publish()
        self.assertEqual(gateway_publish.verify_bundle(self.bundle), provenance)

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
            "VERY_PRIVATE_GATEWAY_KEY", "/private/v1", "account_id",
            "123456789012", "credentials must never publish",
        ):
            self.assertNotIn(forbidden, bundle_text)

        public_row = self._public_gateway_row()
        self.assertEqual(
            public_row["proxy_metrics"]["calls"][0]["cache"],
            {
                "cached_input_tokens": 7,
                "cache_write_input_tokens": 3,
            },
        )
        binding = public_row["ledger"]
        self.assertEqual(binding, provenance["ledgers"][self.cell_id])
        seal = json.loads(
            (self.bundle / binding["artifact"]).read_text().splitlines()[-1]
        )
        self.assertEqual(seal["cell_id"], self.cell_id)
        self.assertEqual(seal["root_hash"], binding["root_hash"])
        self.assertEqual(seal["seal_sha256"], binding["seal_sha256"])

    def test_publication_rejects_incomplete_declared_schedule(self):
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "latest matched block is incomplete",
        ):
            gateway_publish._require_complete_schedule(
                self.experiment.to_dict(),
                [self.row],
            )

    def _complete_schedule_rows(self):
        direct_identity = dataclasses.replace(
            self.identity,
            arm_id="direct",
            arm_digest=self.experiment.arms[0].digest,
        )
        direct_row = copy.deepcopy(self.row)
        direct_row["identity"] = direct_identity.as_dict()
        direct_row["cell_id"] = results.make_gateway_cell_id(direct_identity)
        direct_row["run_id"] = results.make_gateway_run_id(direct_identity)
        return [self.row, direct_row]

    def test_publication_accepts_valid_latest_block(self):
        gateway_publish._require_complete_schedule(  # noqa: SLF001
            self.experiment.to_dict(),
            self._complete_schedule_rows(),
        )

    def test_publication_rejects_infrastructure_invalid_latest_cell(self):
        self.row["result"]["infrastructure_invalid_reason"] = "upstream_auth_failure"
        self._write_results(self.row)

        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "latest matched block has infrastructure-invalid cell",
        ):
            self.publish()

    def test_publication_rejects_route_integrity_invalid_latest_cell(self):
        self.row["route_integrity"] = {
            "pass": False,
            "reasons": ["provider_conflict"],
        }
        self._write_results(self.row)

        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "latest matched block has route-integrity-invalid cell",
        ):
            self.publish()

    def test_verify_rejects_infrastructure_invalid_latest_cell(self):
        self.publish()
        self._rewrite_public_gateway_row(
            lambda row: row["result"].update(
                infrastructure_invalid_reason="upstream_auth_failure"
            )
        )

        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "latest matched block has infrastructure-invalid cell",
        ):
            gateway_publish.verify_bundle(self.bundle)

    def test_verify_rejects_route_integrity_invalid_latest_cell(self):
        self.publish()
        self._rewrite_public_gateway_row(
            lambda row: row["route_integrity"].update({
                "pass": False,
                "reasons": ["provider_conflict"],
            })
        )

        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "latest matched block has route-integrity-invalid cell",
        ):
            gateway_publish.verify_bundle(self.bundle)

    def test_publication_binds_rows_to_one_declared_matched_block(self):
        direct_identity = dataclasses.replace(
            self.identity,
            arm_id="direct",
            arm_digest=self.experiment.arms[0].digest,
        )
        direct_row = copy.deepcopy(self.row)
        direct_row["identity"] = direct_identity.as_dict()
        direct_row["cell_id"] = results.make_gateway_cell_id(direct_identity)
        direct_row["run_id"] = results.make_gateway_run_id(direct_identity)
        rows = [self.row, direct_row]
        gateway_publish._require_complete_schedule(  # noqa: SLF001
            self.experiment.to_dict(),
            rows,
        )

        wrong_block = dataclasses.replace(direct_identity, block_id="other-block")
        direct_row["identity"] = wrong_block.as_dict()
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "conflicting block IDs",
        ):
            gateway_publish._require_complete_schedule(  # noqa: SLF001
                self.experiment.to_dict(),
                rows,
            )

        wrong_arm = dataclasses.replace(direct_identity, arm_digest=digest("wrong-arm"))
        direct_row["identity"] = wrong_arm.as_dict()
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "arm digest does not match",
        ):
            gateway_publish._require_complete_schedule(  # noqa: SLF001
                self.experiment.to_dict(),
                rows,
            )

    def test_publication_rejects_reused_cold_prefix_commitment(self):
        evidence = {
            "mode": "isolated_per_call_v1",
            "transform_id": gateway_spec.COLD_PREFIX_TRANSFORM_ID,
            "prefix_injected": True,
            "scope": "forwarded_request",
            "nonce_commitment": "a" * 64,
        }
        cache_metrics = {
            "usage": {"input_tokens_details": {"cached_tokens": 0}},
        }
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "reuses a cold-prefix commitment",
        ):
            gateway_publish._provider_prompt_commitments(
                [
                    {
                        "status": 200,
                        "forwarded_upstream": True,
                        "provider_cache": evidence,
                        "gateway_metrics": cache_metrics,
                    },
                    {
                        "status": 200,
                        "forwarded_upstream": True,
                        "provider_cache": evidence,
                        "gateway_metrics": cache_metrics,
                    },
                ],
                "isolated_per_call_v1",
                "fixture",
            )

    def test_publication_requires_zero_cached_tokens_in_isolated_mode(self):
        evidence = {
            "mode": "isolated_per_call_v1",
            "transform_id": gateway_spec.COLD_PREFIX_TRANSFORM_ID,
            "prefix_injected": True,
            "scope": "forwarded_request",
            "nonce_commitment": "a" * 64,
        }
        for details in (None, {"cached_tokens": 1}):
            with self.subTest(details=details), self.assertRaisesRegex(
                gateway_publish.GatewayPublishError,
                "zero cached-token evidence",
            ):
                gateway_publish._provider_prompt_commitments(
                    [{
                        "status": 200,
                        "forwarded_upstream": True,
                        "provider_cache": evidence,
                        "gateway_metrics": {
                            "usage": {"input_tokens_details": details},
                        },
                    }],
                    "isolated_per_call_v1",
                    "fixture",
                )

    def test_publication_requires_mode_evidence_only_after_forwarding(self):
        gateway_publish._provider_prompt_commitments(  # noqa: SLF001
            [{
                "status": 502,
                "forwarded_upstream": False,
                "provider_cache": None,
            }],
            "isolated_per_call_v1",
            "fixture",
        )
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "provider-default evidence",
        ):
            gateway_publish._provider_prompt_commitments(  # noqa: SLF001
                [{
                    "status": 200,
                    "forwarded_upstream": True,
                    "provider_cache": None,
                }],
                "provider_default",
                "fixture",
            )

    def test_gateway_route_evidence_is_minimized_and_opaque_in_public_ledger(self):
        generation_id = "vercel-generation-raw-id"
        metrics = {
            "timing": {"ttfb_s": 1.0, "semantic_ttft_s": 2.0, "total_s": 3.0},
            "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            "generation": {
                "output_tokens": 4,
                "duration_s": 1.0,
                "tokens_per_second": 4.0,
            },
            "route": {
                "requested_model": "openai/gpt-test",
                "metadata_requested_model": "openai/gpt-test",
                "served_model": "gpt-test-2026-07-22",
                "provider": "OpenAI",
                "attempts": [{
                    "provider": "OpenAI",
                    "model": "gpt-test-2026-07-22",
                    "status": 200,
                }],
                "gateway_metadata": {
                    "generationId": generation_id,
                    "cost": 0.001,
                    "marketCost": 0.002,
                    "unnecessary_payload": "must-not-publish",
                },
            },
            "route_evidence": {"pass": True, "verdict": "pass", "reasons": []},
            "coverage": {
                "covered": 6,
                "total": 6,
                "usage": True,
                "semantic_ttft": True,
                "route": True,
                "attempts": True,
            },
            "stream": {"done": True, "malformed_events": 0, "ignored_events": 0},
        }
        self._write_ledger(gateway_metrics=metrics)
        self.publish()

        public_experiment = json.loads(
            (self.bundle / "experiment.json").read_text(encoding="utf-8")
        )
        public_arm = next(
            arm for arm in public_experiment["arms"] if arm["arm_id"] == "gateway"
        )
        self.assertEqual(public_arm["gateway"], "vercel")
        self.assertEqual(
            public_arm["allowed_models"],
            ["openai/gpt-test", "gpt-test-2026-07-22"],
        )

        public_row = self._public_gateway_row()
        public_ledger = self.bundle / public_row["ledger"]["artifact"]
        request = json.loads(public_ledger.read_text().splitlines()[0])
        route = request["gateway_metrics"]["route"]
        self.assertEqual(route["served_model"], "gpt-test-2026-07-22")
        self.assertEqual(route["provider"], "OpenAI")
        self.assertEqual(route["attempts"], [{
            "provider": "OpenAI",
            "model": "gpt-test-2026-07-22",
            "status": 200,
        }])
        self.assertEqual(route["gateway_metadata"], {
            "generation_id_sha256": hashlib.sha256(
                generation_id.encode("utf-8")
            ).hexdigest(),
            "cost": 0.001,
            "market_cost": 0.002,
        })
        bundle_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.bundle.rglob("*")
            if path.is_file()
        )
        for private_value in (
            generation_id,
            "unnecessary_payload",
            "must-not-publish",
        ):
            self.assertNotIn(private_value, bundle_text)
        self.assertEqual(gateway_publish.verify_bundle(self.bundle), json.loads(
            (self.bundle / "provenance.json").read_text(encoding="utf-8")
        ))

    def test_gateway_metadata_projection_rejects_malformed_generation_id(self):
        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "generationId must be a non-empty string",
        ):
            gateway_publish._gateway_metadata_dto(  # noqa: SLF001
                {"generationId": ""},
                "metadata",
            )
        self.assertEqual(
            gateway_publish._gateway_metadata_dto(  # noqa: SLF001
                {
                    "generationId": "opaque-generation",
                    "raw_payload": "discarded",
                },
                "metadata",
            ),
            {
                "generation_id_sha256": hashlib.sha256(
                    b"opaque-generation"
                ).hexdigest(),
            },
        )

    def test_published_results_round_trip_through_gateway_report(self):
        direct_identity = dataclasses.replace(
            self.identity,
            arm_id="direct",
            arm_digest=self.experiment.arms[0].digest,
        )
        direct = copy.deepcopy(self.row)
        direct["identity"] = direct_identity.as_dict()
        direct["run_id"] = results.make_gateway_run_id(direct_identity)
        direct["cell_id"] = results.make_gateway_cell_id(direct_identity)
        direct["arm_role"] = "direct"
        direct["baseline"] = True
        direct["result"]["duration_s"] = 10.0
        direct["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 0.5
        direct["proxy_metrics"]["calls"][0]["timing"]["semantic_ttft_s"] = 1.0
        direct["proxy_metrics"]["calls"][0]["route"]["provider"] = "Direct"
        direct_ledger = self.root / "direct-ledger.jsonl"
        request = json.loads(self.source_ledger.read_text().splitlines()[0])
        request["serving_arm"] = {
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
        gateway_publish.publish_bundle(
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

        expected = gateway_report.aggregate(
            source_rows, bootstrap_replicates=20, bootstrap_seed=7
        )
        actual = gateway_report.aggregate(
            public_rows, bootstrap_replicates=20, bootstrap_seed=7
        )
        self.assertEqual(actual, expected)

    def test_max_calls_outcome_reason_survives_publish(self):
        self.row["result"].update(
            solved=False,
            checker_score=0.0,
            available=True,
            budget_exhausted_reason="max_calls",
        )
        self._write_results(self.row)

        provenance = self.publish()

        self.assertEqual(gateway_publish.verify_bundle(self.bundle), provenance)
        public_row = self._public_gateway_row()
        self.assertEqual(
            public_row["result"]["budget_exhausted_reason"],
            "max_calls",
        )
        self.assertIsNone(public_row["result"]["infrastructure_invalid_reason"])
        self.assertFalse(public_row["result"]["solved"])
        self.assertTrue(public_row["result"]["available"])

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
        self.assertEqual(gateway_publish.verify_bundle(self.bundle), provenance)
        public_row = self._public_gateway_row()
        public_call = public_row["proxy_metrics"]["calls"][0]
        self.assertNotIn("timing", public_call)
        self.assertIsNone(public_call["generation"])
        self.assertNotIn("route", public_call)
        self.assertIsNone(public_call["costs"])
        public_ledger = self.bundle / public_row["ledger"]["artifact"]
        request = json.loads(public_ledger.read_text().splitlines()[0])
        self.assertIsNone(request["usage"])
        self.assertIsNone(request["gateway_metrics"])

    def test_partial_call_shape_validation_remains_fail_closed(self):
        self.row["proxy_metrics"]["calls"][0]["generation"] = "unknown"
        self._write_results(self.row)

        with self.assertRaisesRegex(
            gateway_publish.GatewayPublishError,
            "generation must be an object",
        ):
            self.publish()

    def test_publish_rejects_corrupt_jsonl_and_cell_ledger_mismatch(self):
        self.results_path.write_bytes(self.results_path.read_bytes()[:-1])
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "newline"):
            self.publish()

        self._write_results(self.row)
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "bindings mismatch"):
            gateway_publish.publish_bundle(
                self.results_path,
                self.bundle,
                experiment=self.experiment,
                policy=self.policy,
                catalog=self.catalog,
                prices=self.prices,
                ledgers={"gateway-cell-v2-wrong": self.source_ledger},
            )

    def test_publish_rejects_unsealed_or_tampered_source_ledger(self):
        lines = self.source_ledger.read_text().splitlines()
        self.source_ledger.write_text(lines[0] + "\n", encoding="utf-8")
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "terminal seal"):
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
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "tampered"):
            self.publish()

    def test_publish_rejects_source_ledger_not_bound_to_result(self):
        self.row["ledger_seal"]["root_hash"] = "0" * 64
        self._write_results(self.row)
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "does not match"):
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
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "digest mismatch"):
            gateway_publish.verify_bundle(self.bundle)

        results_path.write_text(canonical_line(
            gateway_publish._decode_json(  # noqa: SLF001 - restore test fixture
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
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "artifact set mismatch"):
            gateway_publish.verify_bundle(self.bundle)

        (self.bundle / "unexpected.txt").unlink()
        price_path = self.bundle / "prices.json"
        price_raw = price_path.read_bytes()
        price_path.unlink()
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "artifact set mismatch"):
            gateway_publish.verify_bundle(self.bundle)
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
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "ledger binding mismatch"):
            gateway_publish.verify_bundle(self.bundle)

    def test_verify_rejects_corrupt_jsonl_even_when_artifact_hash_is_updated(self):
        self.publish()
        path = self.bundle / "results.jsonl"
        raw = path.read_bytes()[:-1]
        path.write_bytes(raw)
        provenance_path = self.bundle / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["artifacts"]["results.jsonl"] = hashlib.sha256(raw).hexdigest()
        provenance_path.write_text(canonical_line(provenance), encoding="utf-8")
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "newline"):
            gateway_publish.verify_bundle(self.bundle)

    def test_secret_in_allowlisted_value_is_rejected_on_publish_and_verify(self):
        self._write_ledger(allowed_secret="sk-abcdefghijklmnopqrstuvwxyz")
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "credential pattern"):
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
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "email"):
            gateway_publish.verify_bundle(self.bundle)

    def test_snapshot_digest_mismatch_fails_closed(self):
        wrong = dataclasses.replace(self.identity, policy_digest=digest("wrong-policy"))
        row = dict(self.row)
        row["identity"] = wrong.as_dict()
        row["run_id"] = results.make_gateway_run_id(wrong)
        row["cell_id"] = results.make_gateway_cell_id(wrong)
        self._write_results(row)
        with self.assertRaisesRegex(gateway_publish.GatewayPublishError, "snapshot digests"):
            gateway_publish.publish_bundle(
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
