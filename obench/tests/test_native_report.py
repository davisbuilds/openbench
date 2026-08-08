from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from obench.native_matrix import build_native_matrix
from obench.mcp_stdio_collector import CallLedger
from obench.native_report import (
    NativeReportError,
    _Observation,
    _aggregate_observations,
    _load_bundle,
    _merge_observation,
    _matched_deltas,
    assert_public_native_report,
    build_native_report,
)
from obench.native_trial import BUNDLE_SCHEMA_VERSION
from obench.run import ROW_FIELDS, make_run_id
from obench.tests.test_native_trial import (
    FIXTURE_CASES,
    _build_bundle,
    _reseal_manifest,
)


HARNESS = {
    "name": "codex",
    "version": "0.200.0",
    "version_source": "command",
    "config_sha256": "a" * 64,
}
MODEL = {"name": "gpt-fixture", "provider": "openai", "snapshot": "2026-08-06"}
TASK = {"name": "native-form", "content_sha256": "b" * 64}
MCP_A = {
    "name": "computer-use-mcp",
    "version": "1.0.0",
    "transport": "stdio",
    "server_sha256": "c" * 64,
    "collector_run_id": "collector-a",
}
MCP_B = {
    "name": "computer-use-mcp",
    "version": "1.1.0",
    "transport": "stdio",
    "server_sha256": "d" * 64,
    "collector_run_id": "collector-b",
}


def _plan(repetitions=2):
    return build_native_matrix(
        comparison_id="cub-v0",
        task=TASK,
        harness=HARNESS,
        model=MODEL,
        arms=[
            {"id": "baseline", "mcp": MCP_A},
            {"id": "candidate", "mcp": MCP_B},
        ],
        repetitions=repetitions,
    )


def _row(plan, arm_id, block, **overrides):
    arm = next(arm for arm in plan["arms"] if arm["id"] == arm_id)
    cell = next(
        cell
        for cell in plan["schedule"]
        if cell["arm_id"] == arm_id and cell["block"] == block
    )
    row = {field: None for field in ROW_FIELDS}
    row.update(
        {
            "run_id": make_run_id(
                HARNESS["name"],
                TASK["name"],
                MODEL["name"],
                block,
                candidate_digest="5" * 64,
                full_candidate_digest=True,
            ),
            "ts_iso": f"2026-08-{block:02d}T12:00:00+00:00",
            "harness": HARNESS["name"],
            "model": MODEL["name"],
            "task": TASK["name"],
            "trial": block,
            "success": True,
            "completed": True,
            "error": None,
            "wall_time_s": 10.0,
            "t_env_setup_s": 1.0,
            "t_agent_s": 8.0,
            "t_checker_s": 1.0,
            "tokens": 100,
            "tokens_input_uncached": 70,
            "tokens_cache_read": 20,
            "tokens_cache_write": None,
            "tokens_output": 30,
            "tokens_reasoning": None,
            "usage_raw": {
                "source": "native_atif",
                "input_tokens": 90,
                "cached_tokens": 20,
                "output_tokens": 30,
            },
            "token_basis": "native_atif",
            "tokens_fresh": 100,
            "turns": 2,
            "checker_exit": 0,
            "exec_mode": "native_macos",
            "score": 1.0,
            "harness_version": HARNESS["version"],
            "harness_version_source": HARNESS["version_source"],
            "failure_class": "solved",
            "candidate_provenance": {
                "kind": "native_macos_trial",
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial_id": cell["trial_id"],
                "lock_sha256": "1" * 64,
                "terminal_evidence_root": "6" * 64,
                "result_identity_sha256": "5" * 64,
                "result_sha256": f"{block:064x}",
                "manifest_sha256": (
                    ("a" if arm_id == "baseline" else "b") * 64
                ),
                "task_content_sha256": TASK["content_sha256"],
                "mcp_ledger_sha256": "2" * 64,
                "mcp_root_hash": "3" * 64,
                "harness_identity": HARNESS,
                "model_identity": MODEL,
                "mcp_identity": arm["config_identity"]["mcp"],
                "environment_identity": {"platform": "macos"},
                "phase_timings": {
                    "env_setup_s": 1.0,
                    "agent_s": 8.0,
                    "verifier_s": 1.0,
                    "total_s": 10.0,
                },
                "retry_count": 0,
                "max_retries": 1,
                "terminal_status": None,
                "focus_event_count": 2,
                "mcp_event_count": 3,
                "proxy_measured": True,
            },
            "version_drift": False,
            "timeout_s": 300.0,
            "workspace_source": {
                "kind": "native_final_state",
                "sha256": "4" * 64,
            },
            "usage_evidence_grade": "proxy_reconciled",
            "usage_ranking_eligible": True,
        }
    )
    row.update(overrides)
    uncached = row["tokens_input_uncached"]
    cached = row["tokens_cache_read"]
    output = row["tokens_output"]
    row["tokens"] = uncached + output
    row["tokens_fresh"] = uncached + output
    row["usage_raw"] = {
        "source": "native_atif",
        "input_tokens": uncached + cached,
        "cached_tokens": cached,
        "output_tokens": output,
    }
    row["candidate_provenance"]["phase_timings"] = {
        "env_setup_s": row["t_env_setup_s"],
        "agent_s": row["t_agent_s"],
        "verifier_s": row["t_checker_s"],
        "total_s": row["wall_time_s"],
    }
    if row["completed"] and not row["success"]:
        row["checker_exit"] = 1
        row["failure_class"] = "wrong_answer"
    return row


def _call(tool, latency, *, outcome="success", tier="tier1-ax-action"):
    return {
        "tool": tool,
        "duration_ms": latency,
        "tool_is_error": False,
        "jsonrpc_error": {"present": False, "code": None},
        "computer_use_meta": {
            "error": None,
            "outcome": {
                "classification": outcome,
                "failure_domain": None,
            },
            "delivery": {
                "delivery_tier": tier,
                "fallback_reasons": [],
                "chain_rung": None,
            },
            "focus": {"focus_changed": False},
        },
    }


def _metric_call(tool, latency, metrics):
    call = _call(tool, latency)
    call["computer_use_meta"]["metrics"] = {
        "schema_version": 1,
        **metrics,
    }
    return call


def _replace_bundle_mcp_ledger(bundle, calls):
    ledger_path = bundle / "mcp/ledger.jsonl"
    ledger_path.unlink()
    ledger = CallLedger(
        ledger_path,
        "native-cub-v0-run",
        "native-cub-v0-trial1",
    )
    for sequence, call in enumerate(calls, 1):
        request_unix_ns = 1786017603000000000 + sequence * 1000000000
        ledger.append_call({
            **call,
            "status": "completed",
            "request_id_type": "str",
            "argument_digest": "sha256:" + str(sequence) * 64,
            "request_bytes": 100,
            "response_bytes": 80,
            "request_unix_ns": request_unix_ns,
            "response_unix_ns": request_unix_ns + int(call["duration_ms"] * 1e6),
            "process_returncode": None,
        })
    ledger.seal({
        "returncode": 0,
        "integrity_ok": True,
        "malformed_frames": 0,
        "partial_frames": 0,
        "duplicate_request_ids": 0,
        "missing_responses": 0,
        "relay_failures": 0,
        "input_incomplete": False,
    })
    result_path = bundle / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["mcp_event_count"] = len(calls)
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    _reseal_manifest(bundle)


class NativeReportTests(unittest.TestCase):
    def test_validated_bundle_supplies_digest_bound_mcp_detail(self):
        cases = json.loads(FIXTURE_CASES.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="native_report_bundle_") as temp:
            bundle = Path(temp) / "native-cub-v0-trial1"
            _build_bundle(bundle, cases["happy"])
            observation = _load_bundle(bundle)

        self.assertEqual(observation.row["exec_mode"], "native_macos")
        self.assertEqual(len(observation.mcp_calls), 1)
        self.assertEqual(observation.mcp_calls[0]["tool"], "set_value")
        self.assertEqual(len(observation.proxy_requests), 1)
        self.assertEqual(observation.proxy_requests[0]["duration_ms"], 1000.0)
        self.assertEqual(
            observation.bundle_sha256,
            observation.row["candidate_provenance"]["manifest_sha256"],
        )

    def test_validated_bundle_reports_model_and_mcp_cost_centers(self):
        cases = json.loads(FIXTURE_CASES.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="native_report_costs_") as temp:
            bundle = Path(temp) / "native-cub-v0-trial1"
            _build_bundle(bundle, cases["happy"])
            aggregate = _aggregate_observations([_load_bundle(bundle)])

        self.assertEqual(aggregate["model"]["requests_total"], 1)
        self.assertEqual(aggregate["model"]["latency_ms"]["p50"], 1000.0)
        self.assertEqual(
            aggregate["model"]["input_tokens_per_request"]["median"],
            100,
        )
        self.assertEqual(aggregate["mcp"]["response_bytes"]["total"], 80)

    def test_validated_bundle_aggregates_sealed_mcp_call_metrics(self):
        cases = json.loads(FIXTURE_CASES.read_text(encoding="utf-8"))
        operation = {
            "operation": "click",
            "tool": "click",
            "attempted_delivery_strategies": ["ax-action"],
            "queue_latency_ms": 3,
            "execution_latency_ms": 17,
        }
        perception = {
            "operation": "get_app_state",
            "tool": "get_app_state",
            "elapsed_ms": 41,
            "elements_visited": 120,
            "elements_returned": 35,
            "partial": False,
            "diff": True,
            "context_bytes": 4096,
        }
        with tempfile.TemporaryDirectory(prefix="native_report_metrics_") as temp:
            bundle = Path(temp) / "native-cub-v0-trial1"
            _build_bundle(
                bundle,
                {
                    **cases["happy"],
                    "allowed_delivery_tiers": ["tier1-ax-action"],
                },
            )
            _replace_bundle_mcp_ledger(
                bundle,
                [
                    _metric_call("click", 20.0, {"operation": operation}),
                    _metric_call(
                        "get_app_state", 50.0, {"perception": perception}
                    ),
                ],
            )
            aggregate = _aggregate_observations([_load_bundle(bundle)])

        mcp = aggregate["mcp"]
        self.assertEqual(mcp["operation_metrics_calls"], 1)
        self.assertEqual(mcp["perception_metrics_calls"], 1)
        self.assertEqual(mcp["queue_latency_ms"]["median"], 3.0)
        self.assertEqual(mcp["queue_latency_ms"]["missing_n"], 0)
        self.assertEqual(mcp["execution_latency_ms"]["p95"], 17.0)
        self.assertEqual(mcp["elapsed_ms"]["median"], 41.0)
        self.assertEqual(mcp["context_bytes"]["total"], 4096.0)
        self.assertIsInstance(mcp["context_bytes"]["total"], int)
        self.assertEqual(mcp["elements_visited"]["total"], 120.0)
        self.assertEqual(mcp["elements_returned"]["median"], 35.0)
        self.assertEqual(
            mcp["partial_rate"],
            {"n": 1, "missing_n": 0, "true_count": 0, "rate": 0.0},
        )
        self.assertEqual(
            mcp["diff_rate"],
            {"n": 1, "missing_n": 0, "true_count": 1, "rate": 1.0},
        )

    def test_per_trial_exclusive_attribution_and_matched_deltas(self):
        plan = _plan(repetitions=1)
        perception = {
            "operation": "set_value",
            "tool": "set_value",
            "perception_ms": 40,
            "settle_ms": 2,
            "screenshot_ms": 0,
            "snapshot_ms": 25,
            "verification_ms": 3,
            "response_construction_ms": 8,
            "other_ms": 2,
            "elements_visited": 10,
            "elements_returned": 8,
            "partial": False,
            "response_encoding": "full",
            "text_bytes": 1000,
            "screenshot_png_bytes": 0,
        }
        operation = {
            "operation": "set_value",
            "tool": "set_value",
            "attempted_delivery_strategies": ["ax-attribute"],
            "queue_latency_ms": 5,
            "execution_latency_ms": 15,
        }
        call = _metric_call(
            "set_value", 60.0, {"operation": operation, "perception": perception}
        )
        call["computer_use_meta"]["metrics"]["schema_version"] = 2
        call["response_bytes"] = 1500
        call["request_unix_ns"] = 2_000_000_000
        call["response_unix_ns"] = 2_060_000_000
        proxy = ({
            "request_unix_ns": 500_000_000,
            "response_unix_ns": 1_500_000_000,
            "duration_ms": 1000.0,
            "paced_wait_ms": 200.0,
            "status": 200,
            "usage_available": True,
            "input_tokens": 100,
            "cached_tokens": 20,
            "output_tokens": 30,
            "error_present": False,
        },)
        baseline = _Observation(
            row=_row(plan, "baseline", 1),
            mcp_calls=(call,),
            proxy_requests=proxy,
            bundle_sha256="a" * 64,
            result_sha256="b" * 64,
            row_sha256="c" * 64,
        )
        candidate_call = json.loads(json.dumps(call))
        candidate_call["duration_ms"] = 50.0
        candidate_call["response_bytes"] = 1200
        candidate_call["computer_use_meta"]["metrics"]["perception"]["text_bytes"] = 700
        candidate = _Observation(
            row=_row(plan, "candidate", 1),
            mcp_calls=(candidate_call,),
            proxy_requests=proxy,
            bundle_sha256="d" * 64,
            result_sha256="e" * 64,
            row_sha256="f" * 64,
        )

        attribution = _aggregate_observations([baseline])["attribution"]
        trial = attribution["trial_totals"][0]
        self.assertEqual(trial["exclusive_time_ms"], {
            "model_api_time_ms": 800.0,
            "paced_wait_ms": 200.0,
            "mcp_time_ms": 60.0,
            "residual_agent_time_ms": 6940.0,
        })
        self.assertEqual(trial["mcp_detail_ms"]["queue_time_ms"], 5.0)
        self.assertEqual(
            trial["mcp_detail_ms"]["perception_phase_time_ms"],
            {
                "other_ms": 2.0,
                "response_construction_ms": 8.0,
                "screenshot_ms": 0.0,
                "settle_ms": 2.0,
                "snapshot_ms": 25.0,
                "verification_ms": 3.0,
            },
        )
        self.assertEqual(trial["bytes"], {
            "response_bytes": 1500,
            "text_bytes": 1000,
            "screenshot_png_bytes": 0,
        })
        self.assertEqual(
            trial["provider_tokens"], {"input": 100, "cached": 20, "output": 30}
        )
        deltas = _matched_deltas([baseline], [candidate])[
            "candidate_minus_reference"
        ]
        self.assertEqual(deltas["mcp_time_ms"]["median"], -10.0)
        self.assertEqual(deltas["response_bytes"]["median"], -300.0)
        self.assertEqual(deltas["text_bytes"]["median"], -300.0)
        self.assertEqual(
            deltas["perception_phase_time_ms.snapshot_ms"]["median"], 0.0
        )

    def test_attribution_rejects_overlapping_or_partial_contracted_evidence(self):
        plan = _plan(repetitions=1)
        row = _row(plan, "baseline", 1)
        row["candidate_provenance"]["mcp_identity"] = {
            **row["candidate_provenance"]["mcp_identity"],
            "call_contract": [{
                "tool": "set_value",
                "required_arguments": {"include_state": True},
            }],
        }
        call = _call("set_value", 100.0)
        call.update({
            "request_unix_ns": 1_000_000_000,
            "response_unix_ns": 1_100_000_000,
            "response_bytes": 100,
        })
        request = {
            "request_unix_ns": 900_000_000,
            "response_unix_ns": 1_050_000_000,
            "duration_ms": 150.0,
            "paced_wait_ms": 0.0,
            "status": 200,
            "usage_available": True,
            "input_tokens": 1,
            "cached_tokens": 0,
            "output_tokens": 1,
            "error_present": False,
        }
        observation = _Observation(
            row=row,
            mcp_calls=(call,),
            proxy_requests=(request,),
            bundle_sha256="a" * 64,
            result_sha256="b" * 64,
            row_sha256="c" * 64,
        )
        with self.assertRaisesRegex(NativeReportError, "overlapping sealed intervals"):
            _aggregate_observations([observation])

        request["response_unix_ns"] = 950_000_000
        request["duration_ms"] = 50.0
        with self.assertRaisesRegex(NativeReportError, "missing perception telemetry"):
            _aggregate_observations([observation])

    def test_validated_bundle_rejects_invalid_sealed_mcp_metrics(self):
        cases = json.loads(FIXTURE_CASES.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="native_report_bad_metrics_") as temp:
            bundle = Path(temp) / "native-cub-v0-trial1"
            _build_bundle(
                bundle,
                {
                    **cases["happy"],
                    "allowed_delivery_tiers": ["tier1-ax-action"],
                },
            )
            _replace_bundle_mcp_ledger(
                bundle,
                [
                    _metric_call(
                        "set_value",
                        20.0,
                        {
                            "operation": {
                                "operation": "set_value",
                                "tool": "set_value",
                                "attempted_delivery_strategies": ["ax-attribute"],
                                "queue_latency_ms": 3,
                                "execution_latency_ms": 17,
                            }
                        },
                    )
                ],
            )
            records = [
                json.loads(line)
                for line in (bundle / "mcp/ledger.jsonl").read_text().splitlines()
            ]
            records[0]["computer_use_meta"]["metrics"]["schema_version"] = 3
            _replace_bundle_mcp_ledger(bundle, [records[0]])

            with self.assertRaisesRegex(NativeReportError, "invalid metrics metadata"):
                _load_bundle(bundle)

    def test_incomplete_arm_is_excluded_and_surfaced(self):
        plan = _plan(repetitions=2)
        report = build_native_report(
            plan,
            [
                _row(plan, "baseline", 1),
                _row(plan, "candidate", 1, success=False, score=0.0),
                _row(plan, "baseline", 2),
            ],
        )

        self.assertEqual(report["coverage"]["complete_matched_blocks"], 1)
        self.assertEqual(
            report["coverage"]["incomplete_blocks"],
            [{"block": 2, "missing_cell_ids": ["block2:candidate"]}],
        )
        self.assertEqual(report["arms"]["baseline"]["n"], 1)
        self.assertEqual(report["arms"]["candidate"]["n"], 1)
        self.assertEqual(
            report["matched_deltas"]["candidate"]["success_pairs"],
            {"wins": 0, "ties": 0, "losses": 1},
        )
        self.assertEqual(len(report["evidence_digests"]), 3)

    def test_duplicate_rows_conflict_and_bundle_enrichment_is_order_independent(self):
        plan = _plan(repetitions=1)
        baseline = _row(plan, "baseline", 1)
        changed = _row(
            plan,
            "baseline",
            1,
            wall_time_s=11.0,
            t_agent_s=9.0,
        )
        with self.assertRaisesRegex(NativeReportError, "conflicting results"):
            build_native_report(
                plan,
                [baseline, changed, _row(plan, "candidate", 1)],
            )

        row_only = _Observation(
            row=baseline,
            mcp_calls=None,
            bundle_sha256="a" * 64,
            result_sha256="b" * 64,
            row_sha256="c" * 64,
        )
        bundle = _Observation(
            row=baseline,
            mcp_calls=(_call("click", 10.0),),
            bundle_sha256="a" * 64,
            result_sha256="b" * 64,
            row_sha256="c" * 64,
        )
        self.assertIsNotNone(
            _merge_observation(row_only, bundle, cell_id="block1:baseline").mcp_calls
        )
        self.assertIsNotNone(
            _merge_observation(bundle, row_only, cell_id="block1:baseline").mcp_calls
        )

    def test_row_timing_gate_matches_importer_sum_and_rounding_rules(self):
        plan = _plan(repetitions=1)
        impossible = _row(
            plan,
            "baseline",
            1,
            wall_time_s=10.0,
            t_env_setup_s=10.0,
            t_agent_s=10.0,
            t_checker_s=10.0,
        )
        with self.assertRaisesRegex(
            NativeReportError, "phase timings exceed total"
        ):
            build_native_report(
                plan,
                [impossible, _row(plan, "candidate", 1)],
            )

        rounded = _row(plan, "baseline", 1)
        rounded["candidate_provenance"]["phase_timings"]["total_s"] = 10.0013
        report = build_native_report(
            plan,
            [rounded, _row(plan, "candidate", 1)],
        )
        self.assertEqual(report["coverage"]["complete_matched_blocks"], 1)

    def test_metric_math_wilson_token_splits_and_outlier_summary(self):
        plan = _plan(repetitions=10)
        rows = []
        for block in range(1, 11):
            rows.append(_row(plan, "baseline", block))
            candidate_wall = 1000.0 if block == 10 else 20.0
            rows.append(
                _row(
                    plan,
                    "candidate",
                    block,
                    wall_time_s=candidate_wall,
                    t_agent_s=candidate_wall - 2,
                    tokens_input_uncached=140,
                    tokens_cache_read=40,
                    tokens_output=60,
                    turns=4,
                )
            )
        report = build_native_report(plan, rows)
        candidate = report["arms"]["candidate"]

        self.assertEqual(candidate["success"]["count"], 10)
        self.assertEqual(candidate["success"]["rate"], 1.0)
        self.assertLess(candidate["success"]["wilson_95"][0], 1.0)
        self.assertEqual(candidate["metrics"]["wall_time_s"]["median"], 20.0)
        self.assertEqual(candidate["metrics"]["wall_time_s"]["p95"], 1000.0)
        self.assertEqual(
            candidate["metrics"]["tokens_input_uncached"]["median"], 140.0
        )
        self.assertEqual(candidate["metrics"]["tokens_reasoning"]["missing_n"], 10)
        self.assertIsNone(candidate["metrics"]["tokens_reasoning"]["p95"])
        self.assertEqual(candidate["efficiency"]["success_per_fresh_token"], 0.005)
        self.assertEqual(candidate["efficiency"]["success_per_turn"], 0.25)
        self.assertEqual(
            report["matched_deltas"]["candidate"][
                "candidate_minus_reference"
            ]["fresh_tokens"]["median"],
            100.0,
        )

    def test_mcp_tool_latency_category_and_action_math(self):
        plan = _plan(repetitions=1)
        row = _row(plan, "baseline", 1)
        observation = _Observation(
            row=row,
            mcp_calls=(
                _call("click", 10.0),
                _call("click", 20.0),
                _call("get_app_state", 1000.0, outcome="effect_not_verified"),
            ),
            bundle_sha256="a" * 64,
            result_sha256="b" * 64,
            row_sha256="c" * 64,
        )
        aggregate = _aggregate_observations([observation])

        self.assertEqual(
            aggregate["mcp"]["calls_per_tool"],
            {"click": 2, "get_app_state": 1},
        )
        self.assertEqual(aggregate["mcp"]["latency_ms"]["p50"], 20.0)
        self.assertEqual(aggregate["mcp"]["latency_ms"]["p95"], 1000.0)
        self.assertEqual(
            aggregate["mcp"]["outcome_counts"],
            {"effect_not_verified": 1, "success": 2},
        )
        self.assertEqual(
            aggregate["mcp"]["delivery_counts"], {"tier:tier1-ax-action": 3}
        )
        self.assertEqual(
            aggregate["mcp"]["focus_counts"], {"focus_changed:false": 3}
        )
        self.assertEqual(aggregate["efficiency"]["success_per_action"], 1 / 3)

    def test_row_only_mcp_metrics_are_missing_not_invented(self):
        plan = _plan(repetitions=1)
        report = build_native_report(
            plan, [_row(plan, arm, 1) for arm in ("baseline", "candidate")]
        )
        baseline = report["arms"]["baseline"]
        self.assertFalse(baseline["mcp"]["available"])
        self.assertIsNone(baseline["mcp"]["calls_per_tool"])
        self.assertIn(
            "mcp_breakdown_requires_validated_bundles",
            baseline["unavailable_metrics"],
        )
        self.assertEqual(
            report["publication_status"],
            "complete_row_bound_bundle_not_revalidated",
        )
        self.assertEqual(report["coverage"]["row_only_cells"], 2)
        self.assertRegex(
            report["evidence_digests"][0]["normalized_row_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_privacy_rejects_sensitive_rows_and_public_raw_fields(self):
        plan = _plan(repetitions=1)
        sensitive = _row(plan, "baseline", 1)
        sensitive["candidate_provenance"]["environment_identity"][
            "operator"
        ] = "person@example.com"
        with self.assertRaisesRegex(NativeReportError, "email address"):
            build_native_report(
                plan,
                [
                    sensitive,
                    _row(plan, "candidate", 1),
                ],
            )
        with self.assertRaisesRegex(NativeReportError, "forbidden public field"):
            assert_public_native_report({"screenshots": ["digest-only-is-still-raw"]})
        with self.assertRaisesRegex(NativeReportError, "absolute home path"):
            assert_public_native_report({"note": "/Users/person/private.txt"})

    def test_public_report_contains_no_harbor_claim_or_raw_evidence(self):
        plan = _plan(repetitions=1)
        report = build_native_report(
            plan, [_row(plan, arm, 1) for arm in ("baseline", "candidate")]
        )
        encoded = json.dumps(report, sort_keys=True).lower()
        self.assertNotIn("harbor", encoded)
        self.assertNotIn("trajectory", encoded)
        self.assertNotIn('"screenshots"', encoded)
        self.assertEqual(
            report["methodology"]["execution_backend"], "native_macos"
        )
        self.assertEqual(
            report["methodology"]["score_judge"],
            "deterministic_verifier_only",
        )


if __name__ == "__main__":
    unittest.main()
