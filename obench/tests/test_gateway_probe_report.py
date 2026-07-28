import copy
import unittest

from obench import gateway_probe_report, gateway_probe_results


def row(arm, condition, repetition, *, baseline=False, total=1.0, route="verified"):
    identity = {
        "benchmark": {"name": "gateway_probe", "track": "request_probe"},
        "experiment": {"id": "exp", "digest": "e" * 64},
        "arm": {"id": arm, "digest": ("a" if baseline else "b") * 64},
        "case": {"id": "case", "prompt_digest": "c" * 64},
        "comparison": {"schedule_digest": "s" * 64, "price_digest": "p" * 64},
        "schedule": {
            "condition": condition,
            "repetition": repetition,
            "block_id": f"{condition}-{repetition}",
            "block_attempt": 0,
        },
    }
    return {
        "schema_version": gateway_probe_results.RESULT_SCHEMA_VERSION,
        "benchmark": "gateway_probe",
        "cell_id": gateway_probe_results.cell_id(identity),
        "identity": identity,
        "expected_arm_ids": ["direct", "gateway"],
        "scheduled_blocks_per_condition": 2,
        "arm_role": "direct" if baseline else "gateway",
        "baseline": baseline,
        "model_match": "exact_revision",
        "outcome": {
            "attempted": True,
            "success": True,
            "available": True,
            "http_status": 200,
            "timed_out": False,
            "error_class": None,
            "error_detail": None,
            "budget_exhausted_reason": None,
        },
        "route_integrity": {"status": route, "pass": route == "verified", "reasons": []},
        "request_metrics": {
            "setup": (
                {"dns_s": 0.01, "tcp_s": 0.02, "tls_s": 0.03}
                if condition == "cold" else None
            ),
            "timing": {
                "request_to_response_headers_s": total / 4,
                "request_to_first_body_byte_s": total / 3,
                "request_to_semantic_ttft_s": total / 2,
                "request_stream_total_s": total,
                "cold_end_to_end_response_headers_s": (
                    total / 4 + 0.1 if condition == "cold" else None
                ),
                "cold_end_to_end_first_body_byte_s": (
                    total / 3 + 0.1 if condition == "cold" else None
                ),
                "cold_end_to_end_semantic_ttft_s": (
                    total / 2 + 0.1 if condition == "cold" else None
                ),
                "cold_end_to_end_stream_total_s": (
                    total + 0.1 if condition == "cold" else None
                ),
            },
            "receipt_headers": {"x-request-id": f"receipt-{arm}-{repetition}"},
            "generation": {"tokens_per_second": 10.0},
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            "cache": {"cached_input_tokens": None, "cache_write_input_tokens": None},
            "costs": {
                "frozen_list_estimate": {
                    "amount_usd": 0.001,
                    "currency": "USD",
                    "effective_at": "2026-07-25T00:00:00Z",
                }
            },
            "route": {"served_model": "gpt-test", "provider": "openai"},
            "stream": {
                "done": True,
                "terminal_status": "completed",
                "finalized": True,
            },
            "coverage": {},
        },
        "reuse_evidence": {
            "required": condition == "warm",
            "completed": condition == "warm",
            "http_status": 200 if condition == "warm" else None,
            "socket_reused": True if condition == "warm" else None,
            "primer_nonce_sha256": "1" * 64,
            "measured_nonce_sha256": "2" * 64,
            "setup": {"dns_s": 0.01, "tcp_s": 0.02, "tls_s": 0.03},
            "receipt_headers": {},
            "route_integrity": (
                {"status": "verified", "pass": True, "reasons": []}
                if condition == "warm" else None
            ),
            "usage": None,
            "cache": None,
            "costs": {},
        },
        "billing": {
            "primer_cost_usd": "0" if condition == "warm" else None,
            "measured_cost_usd": "0.001",
            "charged_cost_usd": "0.001",
            "observed_cost_usd": "0.001",
            "known_observed_cost_usd": "0.001",
            "budget_debit_usd": "0.001",
            "cost_status": "observed",
            "unknown_cost_attempts": 0,
            "stop_required": False,
        },
        "retry_evidence": {
            "max_total_attempts": 1,
            "max_input_tokens": None,
            "max_output_tokens": 64,
            "retry_deadline_s": None,
            "attempt_count": 1,
            "recovered": False,
            "first_attempt_outcome": {
                "success": True,
                "http_status": 200,
                "timed_out": False,
                "error_class": None,
                "error_detail": None,
                "semantic_output_started": True,
            },
            "eventual_outcome": {
                "success": True,
                "http_status": 200,
                "timed_out": False,
                "error_class": None,
                "error_detail": None,
                "semantic_output_started": True,
            },
            "recovery_timing": {
                "initial_request_to_final_response_headers_s": total / 4,
                "initial_request_to_final_semantic_output_s": total / 2,
                "initial_request_to_completion_s": total,
                "final_attempt_request_start_offset_s": 0.0,
            },
            "attempts": [{
                "attempt_number": 1,
                "phase": "measured",
                "outcome": {
                    "success": True,
                    "http_status": 200,
                    "timed_out": False,
                    "error_class": None,
                    "error_detail": None,
                    "semantic_output_started": True,
                },
                "timing": {
                    "initial_request_start_offset_s": 0.0,
                    "request_to_response_headers_s": total / 4,
                    "request_to_semantic_output_s": total / 2,
                    "attempt_total_s": total,
                },
                "retry": {
                    "eligible": False,
                    "retry_after_status": "absent",
                    "retry_after_s": None,
                    "wait_requested_s": None,
                    "wait_actual_s": None,
                    "not_retried_reason": "semantic_output_started",
                },
                "cost": {
                    "primer_cost_usd": "0" if condition == "warm" else None,
                    "measured_cost_usd": "0.001",
                    "observed_cost_usd": "0.001",
                    "known_observed_cost_usd": "0.001",
                    "budget_debit_usd": "0.001",
                    "reservation_usd": "0",
                    "cost_status": "observed",
                },
            }],
        },
    }


class GatewayProbeReportTests(unittest.TestCase):
    def test_reports_denominators_percentiles_coverage_and_paired_medians(self):
        rows = []
        for condition in ("cold", "warm"):
            for repetition, direct_total in ((1, 1.0), (2, 2.0)):
                rows.append(row("direct", condition, repetition, baseline=True, total=direct_total))
                rows.append(row("gateway", condition, repetition, total=direct_total + 0.5))
        report = gateway_probe_report.aggregate(rows, bootstrap_replicates=100)
        self.assertEqual(report["schema_version"], 5)
        self.assertNotIn("label", report)
        cold = report["arms"]["gateway"]["conditions"]["cold"]
        self.assertEqual(cold["denominators"], {
            "scheduled": 2,
            "attempted": 2,
            "success": 2,
            "request_failed": 0,
            "route_verified": 2,
            "route_unverifiable": 0,
            "route_failed": 0,
        })
        self.assertEqual(cold["retry_diagnostics"], {
            "logical_cells": 2,
            "first_attempt_successes": 2,
            "eventual_successes": 2,
            "retry_rescues": 0,
            "unknown_cost_attempts": 0,
            "attempt_count_distribution": {"1": 2},
        })
        self.assertEqual(cold["metrics"]["request_stream_total_s"]["p50"], 2.0)
        availability = cold["availability"]
        self.assertEqual(availability["successes"], 2)
        self.assertEqual(availability["attempted"], 2)
        self.assertEqual(availability["rate"], 1.0)
        self.assertEqual(availability["wilson95"]["confidence"], 0.95)
        self.assertAlmostEqual(availability["wilson95"]["low"], 0.34237195288961925)
        self.assertEqual(availability["wilson95"]["high"], 1.0)
        self.assertEqual(
            cold["metrics"]["cached_input_tokens"]["coverage"]["covered"], 0
        )
        contrast = report["paired_contrasts"]["gateway"]["cold"][
            "request_stream_total_s"
        ]
        self.assertEqual(contrast["median_gateway_minus_direct"], 0.5)
        self.assertEqual(contrast["coverage"]["covered"], 2)
        self.assertIsNotNone(contrast["interval"])
        warm_metrics = report["arms"]["gateway"]["conditions"]["warm"]["metrics"]
        self.assertNotIn("setup_dns_s", warm_metrics)
        self.assertNotIn("cold_end_to_end_stream_total_s", warm_metrics)
        self.assertNotIn(
            "setup_dns_s",
            report["paired_contrasts"]["gateway"]["warm"],
        )
        text = gateway_probe_report.render_text(report)
        self.assertTrue(text.startswith("Gateway Probe\nblocks cold=2/2 warm=2/2"))
        self.assertNotIn("exploratory", text)
        self.assertNotIn("confirmatory", text)
        self.assertIn("| Arm | Condition | Success / availability |", text)
        self.assertIn("Request to semantic TTFT p50 / p95", text)
        self.assertIn("Cold setup DNS p50", text)
        self.assertIn("Cold end-to-end response headers p50", text)
        self.assertIn("Total tokens p50 (coverage)", text)
        self.assertIn("Cached input p50 (coverage)", text)
        self.assertIn("Measured cost p50 (coverage)", text)
        self.assertIn("| gateway | cold |", text)
        self.assertIn("8.0 (2/2)", text)
        self.assertIn("n/a (0/2)", text)
        self.assertIn("$0.001000 (2/2)", text)
        self.assertIn("| Gateway | Condition | Phase metric |", text)
        self.assertIn(
            "| gateway | cold | Request to semantic TTFT | +0.250s | 2/2 |",
            text,
        )

    def test_missing_metrics_are_not_imputed_and_unverified_rows_do_not_pair(self):
        direct = row("direct", "cold", 1, baseline=True)
        gateway = row("gateway", "cold", 1, route="unverifiable")
        gateway["request_metrics"]["timing"]["request_to_semantic_ttft_s"] = None
        gateway["request_metrics"]["timing"][
            "cold_end_to_end_semantic_ttft_s"
        ] = None
        gateway["outcome"].update({
            "success": False,
            "available": False,
            "error_class": "stream",
            "error_detail": "stream_no_semantic_output",
        })
        warm_direct = row("direct", "warm", 1, baseline=True)
        warm_gateway = row("gateway", "warm", 1)
        rows = [direct, gateway, warm_direct, warm_gateway]
        for item in rows:
            item["scheduled_blocks_per_condition"] = 1
        report = gateway_probe_report.aggregate(rows, bootstrap_replicates=20)
        cold = report["arms"]["gateway"]["conditions"]["cold"]
        self.assertEqual(cold["denominators"]["route_unverifiable"], 1)
        self.assertEqual(
            cold["metrics"]["request_to_semantic_ttft_s"]["coverage"],
            {"covered": 0, "total": 0, "ratio": 0.0},
        )
        self.assertIsNone(
            report["paired_contrasts"]["gateway"]["cold"]["request_stream_total_s"][
                "median_gateway_minus_direct"
            ]
        )
        text = gateway_probe_report.render_text(report)
        self.assertIn("n/a", text)

    def test_unverified_success_counts_as_available_but_not_as_fixed_route_metric(self):
        rows = [
            row("direct", "cold", 1, baseline=True),
            row("gateway", "cold", 1, route="unverifiable"),
            row("direct", "warm", 1, baseline=True),
            row("gateway", "warm", 1),
        ]
        for item in rows:
            item["scheduled_blocks_per_condition"] = 1
        report = gateway_probe_report.aggregate(rows, bootstrap_replicates=20)
        cold = report["arms"]["gateway"]["conditions"]["cold"]
        self.assertEqual(cold["denominators"]["success"], 1)
        self.assertEqual(cold["availability"]["rate"], 1.0)
        self.assertEqual(
            cold["metrics"]["request_stream_total_s"]["coverage"],
            {"covered": 0, "total": 1, "ratio": 0.0},
        )
        self.assertIsNone(cold["metrics"]["request_stream_total_s"]["p50"])
        self.assertEqual(
            cold["metrics"]["total_tokens"]["coverage"],
            {"covered": 0, "total": 1, "ratio": 0.0},
        )
        self.assertEqual(
            cold["metrics"]["measured_cost_usd"]["coverage"],
            {"covered": 0, "total": 1, "ratio": 0.0},
        )

    def test_rejects_mixed_comparison_duplicate_logical_and_provenance_drift(self):
        direct = row("direct", "cold", 1, baseline=True)
        gateway = row("gateway", "cold", 1)
        warm_direct = row("direct", "warm", 1, baseline=True)
        warm_gateway = row("gateway", "warm", 1)
        base = [direct, gateway, warm_direct, warm_gateway]
        for item in base:
            item["scheduled_blocks_per_condition"] = 1

        mixed_price = copy.deepcopy(base)
        mixed_price[-1]["identity"]["comparison"]["price_digest"] = "x" * 64
        mixed_price[-1]["cell_id"] = gateway_probe_results.cell_id(
            mixed_price[-1]["identity"]
        )
        with self.assertRaisesRegex(
            gateway_probe_report.GatewayProbeReportError,
            "schedule or price digests",
        ):
            gateway_probe_report.aggregate(mixed_price)

        duplicate = copy.deepcopy(base)
        duplicate_row = copy.deepcopy(duplicate[0])
        duplicate.append(duplicate_row)
        with self.assertRaisesRegex(
            gateway_probe_report.GatewayProbeReportError,
            "duplicate logical arm row",
        ):
            gateway_probe_report.aggregate(duplicate)

        metadata_drift = copy.deepcopy(base)
        metadata_drift[2]["identity"]["arm"]["digest"] = "z" * 64
        metadata_drift[2]["cell_id"] = gateway_probe_results.cell_id(
            metadata_drift[2]["identity"]
        )
        with self.assertRaisesRegex(
            gateway_probe_report.GatewayProbeReportError,
            "inconsistent arm provenance",
        ):
            gateway_probe_report.aggregate(metadata_drift)

        model_drift = copy.deepcopy(base)
        model_drift[2]["model_match"] = "rolling_alias"
        with self.assertRaisesRegex(
            gateway_probe_report.GatewayProbeReportError,
            "inconsistent arm provenance",
        ):
            gateway_probe_report.aggregate(model_drift)

        tampered_cell = copy.deepcopy(base)
        tampered_cell[0]["cell_id"] = "forged"
        with self.assertRaisesRegex(
            gateway_probe_report.GatewayProbeReportError,
            "cell_id does not match",
        ):
            gateway_probe_report.aggregate(tampered_cell)

        missing_arm = copy.deepcopy(base[:1])
        with self.assertRaisesRegex(
            gateway_probe_report.GatewayProbeReportError,
            "omit metadata for expected arms",
        ):
            gateway_probe_report.aggregate(missing_arm)


if __name__ == "__main__":
    unittest.main()
