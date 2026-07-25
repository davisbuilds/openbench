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
        "schema_version": 2,
        "benchmark": "gateway_probe",
        "cell_id": gateway_probe_results.cell_id(identity),
        "identity": identity,
        "expected_arm_ids": ["direct", "gateway"],
        "scheduled_blocks_per_condition": 2,
        "arm_role": "direct" if baseline else "gateway",
        "baseline": baseline,
        "model_match": "exact_revision",
        "outcome": {"attempted": True, "success": True},
        "route_integrity": {"status": route, "pass": route == "verified", "reasons": []},
        "request_metrics": {
            "connection": {"dns_s": 0.01, "tcp_s": 0.02, "tls_s": 0.03},
            "timing": {
                "ttfb_s": total / 4,
                "semantic_ttft_s": total / 2,
                "total_s": total,
            },
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
        self.assertEqual(report["label"], "exploratory")
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
        self.assertEqual(cold["metrics"]["total_s"]["p50"], 2.0)
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
        contrast = report["paired_contrasts"]["gateway"]["cold"]["total_s"]
        self.assertEqual(contrast["median_gateway_minus_direct"], 0.5)
        self.assertEqual(contrast["coverage"]["covered"], 2)
        self.assertIsNotNone(contrast["interval"])
        text = gateway_probe_report.render_text(report)
        self.assertIn("Gateway Probe (exploratory)", text)
        self.assertIn("| Arm | Condition | Success / availability |", text)
        self.assertIn("Semantic TTFT p50 / p95", text)
        self.assertIn("Measured cost p50 (coverage)", text)
        self.assertIn("| gateway | cold |", text)
        self.assertIn("$0.001000 (2/2)", text)
        self.assertIn("| Gateway | Condition | Median delta TTFT |", text)
        self.assertIn("| gateway | cold | +0.250s | +0.125s | +0.500s | 2/2 |", text)

    def test_missing_metrics_are_not_imputed_and_unverified_rows_do_not_pair(self):
        direct = row("direct", "cold", 1, baseline=True)
        gateway = row("gateway", "cold", 1, route="unverifiable")
        gateway["request_metrics"]["timing"]["semantic_ttft_s"] = None
        warm_direct = row("direct", "warm", 1, baseline=True)
        warm_gateway = row("gateway", "warm", 1)
        rows = [direct, gateway, warm_direct, warm_gateway]
        for item in rows:
            item["scheduled_blocks_per_condition"] = 1
        report = gateway_probe_report.aggregate(rows, bootstrap_replicates=20)
        cold = report["arms"]["gateway"]["conditions"]["cold"]
        self.assertEqual(cold["denominators"]["route_unverifiable"], 1)
        self.assertEqual(
            cold["metrics"]["semantic_ttft_s"]["coverage"],
            {"covered": 0, "total": 1, "ratio": 0.0},
        )
        self.assertIsNone(
            report["paired_contrasts"]["gateway"]["cold"]["total_s"][
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
            cold["metrics"]["total_s"]["coverage"],
            {"covered": 0, "total": 1, "ratio": 0.0},
        )
        self.assertIsNone(cold["metrics"]["total_s"]["p50"])
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
