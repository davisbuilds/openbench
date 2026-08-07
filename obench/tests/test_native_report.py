from __future__ import annotations

import json
import unittest

from obench.native_matrix import build_native_matrix
from obench.native_report import (
    NativeReportError,
    _Observation,
    _aggregate_observations,
    assert_public_native_report,
    build_native_report,
)
from obench.native_trial import BUNDLE_SCHEMA_VERSION
from obench.run import ROW_FIELDS


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
            "run_id": f"{arm_id}-run-{block}",
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
            "tokens_cache_write": 0,
            "tokens_output": 30,
            "tokens_reasoning": 10,
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


class NativeReportTests(unittest.TestCase):
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
                    tokens_reasoning=20,
                    tokens=200,
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
        self.assertEqual(candidate["metrics"]["tokens_reasoning"]["p95"], 20.0)
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

    def test_privacy_rejects_sensitive_rows_and_public_raw_fields(self):
        plan = _plan(repetitions=1)
        with self.assertRaisesRegex(NativeReportError, "email address"):
            build_native_report(
                plan,
                [
                    _row(plan, "baseline", 1, error="contact person@example.com"),
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
        self.assertNotIn("screenshot", encoded)
        self.assertEqual(
            report["methodology"]["execution_backend"], "native_macos"
        )
        self.assertEqual(
            report["methodology"]["score_judge"],
            "deterministic_verifier_only",
        )


if __name__ == "__main__":
    unittest.main()
