"""Contract tests for the schema-v2 Gateway Bench report."""

import copy
import json
import unittest

from obench import results, gateway_report


DIGESTS = {
    name: format(index, "064x")
    for index, name in enumerate(
        (
            "experiment",
            "direct_arm",
            "gateway_arm",
            "policy",
            "catalog",
            "price",
            "sampling",
            "schedule",
            "task_a",
            "task_b",
            "checker",
        ),
        1,
    )
}


def make_row(
    *,
    task,
    arm_id,
    role,
    baseline,
    repetition=1,
    window="w1",
    block_attempt=0,
    solved=True,
    score=1.0,
    available=True,
    duration=10.0,
    calls=None,
    infrastructure_reason=None,
    budget_exhausted_reason=None,
    route_pass=True,
    route_reasons=None,
    track="fixed_model_provider",
    model_match="exact_revision",
):
    block_id = f"{task}-{window}-{repetition}-a{block_attempt}"
    identity = results.CellIdentity.for_gateway(
        track=track,
        experiment_id=f"{track}-fixture",
        experiment_digest=DIGESTS["experiment"],
        arm_id=arm_id,
        arm_digest=DIGESTS[f"{arm_id}_arm"],
        policy_digest=DIGESTS["policy"],
        catalog_digest=DIGESTS["catalog"],
        price_digest=DIGESTS["price"],
        sampling_digest=DIGESTS["sampling"],
        schedule_digest=DIGESTS["schedule"],
        provider_prompt_mode="provider_default",
        task=task,
        task_digest=DIGESTS[task],
        checker_digest=DIGESTS["checker"],
        workspace_source_sha="a" * 40,
        harness="pi",
        candidate=None,
        harness_version="1.0",
        execution_lane="docker",
        image_digest="b" * 64,
        budget_timeout_s=30,
        budget_max_calls=4,
        budget_max_output_tokens=1000,
        budget_usd_cap="1.00",
        adapter_timeout_s=30,
        checker_timeout_s=10,
        window_id=window,
        repetition=repetition,
        block_id=block_id,
        block_attempt=block_attempt,
    )
    result = {
        "solved": solved,
        "checker_score": score,
        "available": available,
        "duration_s": duration,
        "infrastructure_invalid_reason": infrastructure_reason,
        "budget_exhausted_reason": budget_exhausted_reason,
    }
    row = {
        "schema_version": 2,
        "benchmark": "gateway",
        "run_id": results.make_gateway_run_id(identity),
        "cell_id": results.make_gateway_cell_id(identity),
        "identity": identity.as_dict(),
        "arm_role": role,
        "model_match": model_match,
        "provider_prompt_mode": "provider_default",
        "baseline": baseline,
        "result": result,
        "route_integrity": {
            "pass": route_pass,
            "reasons": route_reasons or [],
        },
        "proxy_metrics": {"calls": [] if calls is None else calls},
    }
    return row


def call(
    *,
    provider="OpenAI",
    model="gpt-fixed",
    ttfb=1.0,
    ttft=2.0,
    tokens=10,
    input_tokens=100,
    generation_s=2.0,
    costs=True,
    attempts=None,
    attempts_present=False,
    cache=None,
):
    bases = {}
    if costs:
        bases = {
            "gateway_reported": {
                "amount_usd": 0.10,
                "currency": "USD",
                "effective_at": "2026-07-01T00:00:00Z",
            },
            "frozen_list_estimate": {
                "amount_usd": 0.12,
                "currency": "USD",
                "effective_at": "2026-07-01T00:00:00Z",
            },
        }
    result = {
        "timing": {"ttfb_s": ttfb, "semantic_ttft_s": ttft},
        "generation": {"output_tokens": tokens, "duration_s": generation_s},
        "tokens": {
            "input_tokens": input_tokens,
            "output_tokens": tokens,
            "total_tokens": input_tokens + tokens,
        },
        "route": {
            "provider": provider,
            "served_model": model,
            "attempts": [] if attempts is None else attempts,
            "attempts_present": attempts_present,
        },
        "costs": bases,
    }
    if cache is not None:
        result["cache"] = cache
    return result


class GatewayReportTests(unittest.TestCase):
    def complete_rows(self):
        rows = []
        for task in ("task_a", "task_b"):
            for repetition in (1, 2):
                rows.append(
                    make_row(
                        task=task,
                        arm_id="direct",
                        role="direct",
                        baseline=True,
                        repetition=repetition,
                        duration=10 if task == "task_a" else 20,
                        calls=[call(ttfb=1, ttft=2)],
                    )
                )
                rows.append(
                    make_row(
                        task=task,
                        arm_id="gateway",
                        role="gateway",
                        baseline=False,
                        repetition=repetition,
                        solved=task == "task_a",
                        score=1.0 if task == "task_a" else 0.0,
                        available=not (task == "task_b" and repetition == 2),
                        duration=15 if task == "task_a" else 40,
                        calls=[
                            call(
                                provider="OpenRouter",
                                ttfb=2,
                                ttft=4,
                                tokens=20,
                                generation_s=2,
                            )
                        ],
                    )
                )
        return rows

    def test_aggregates_cells_to_tasks_then_weights_tasks_equally(self):
        report = gateway_report.aggregate(
            self.complete_rows(), bootstrap_replicates=200, bootstrap_seed=7
        )
        direct = report["arms"]["direct"]
        gateway = report["arms"]["gateway"]

        self.assertEqual(report["blocks"], {
            "observed": 4,
            "included": 4,
            "excluded": 0,
            "excluded_by_reason": {},
            "max_calls_affected": 0,
            "max_calls_rate": 0.0,
        })
        self.assertEqual(report["budget"]["max_calls"], 4)
        self.assertEqual(report["tasks"]["included"], 2)
        self.assertEqual(direct["metrics"]["solve_rate"]["estimate"], 1.0)
        self.assertEqual(gateway["metrics"]["solve_rate"]["estimate"], 0.5)
        self.assertEqual(gateway["metrics"]["availability"]["estimate"], 0.75)
        self.assertEqual(gateway["metrics"]["latency_s"]["estimate"], 22.5)
        self.assertEqual(
            gateway["metrics"]["throughput_tokens_per_s"]["estimate"], 10.0
        )
        self.assertEqual(
            report["paired_contrasts"]["gateway"]["metrics"]["latency_s"]["estimate"],
            7.5,
        )
        self.assertEqual(
            report["paired_contrasts"]["gateway"]["metrics"]["solve_rate"]["estimate"],
            -0.5,
        )
        self.assertFalse(report["analysis"]["wilson_intervals"])
        self.assertFalse(report["analysis"]["composite_score"])

    def test_max_calls_incidence_keeps_cells_in_matched_denominator(self):
        rows = self.complete_rows()
        rows[1]["result"].update(
            solved=True,
            checker_score=1.0,
            budget_exhausted_reason="max_calls",
        )

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)

        self.assertEqual(report["blocks"]["included"], 4)
        self.assertEqual(report["blocks"]["max_calls_affected"], 1)
        self.assertEqual(report["blocks"]["max_calls_rate"], 0.25)
        self.assertEqual(report["arms"]["direct"]["max_calls"], {
            "cells": 0,
            "total_cells": 4,
            "ratio": 0.0,
        })
        self.assertEqual(report["arms"]["gateway"]["max_calls"], {
            "cells": 1,
            "total_cells": 4,
            "ratio": 0.25,
        })
        self.assertEqual(
            report["arms"]["gateway"]["metrics"]["solve_rate"]["estimate"],
            0.25,
        )
        rendered = gateway_report.render_text(report)
        self.assertIn("call cap 1/4 (25.0%)", rendered)

    def test_latency_uses_task_weighted_medians(self):
        rows = []
        durations = {
            "task_a": ([1, 1, 1], [2, 3, 100]),
            "task_b": ([2, 2, 2], [4, 5, 6]),
        }
        for task, (direct, gateway) in durations.items():
            for repetition, (direct_s, gateway_s) in enumerate(
                zip(direct, gateway), start=1
            ):
                rows.append(
                    make_row(
                        task=task,
                        arm_id="direct",
                        role="direct",
                        baseline=True,
                        repetition=repetition,
                        duration=direct_s,
                        calls=[call()],
                    )
                )
                rows.append(
                    make_row(
                        task=task,
                        arm_id="gateway",
                        role="gateway",
                        baseline=False,
                        repetition=repetition,
                        duration=gateway_s,
                        calls=[call()],
                    )
                )

        report = gateway_report.aggregate(
            rows, bootstrap_replicates=200, bootstrap_seed=7
        )
        latency = report["arms"]["gateway"]["metrics"]["latency_s"]
        contrast = report["paired_contrasts"]["gateway"]["metrics"]["latency_s"]

        self.assertEqual(latency["estimate"], 4.0)
        self.assertEqual(latency["aggregation"], "median_of_task_medians")
        self.assertEqual(contrast["estimate"], 2.5)
        self.assertEqual(
            contrast["aggregation"],
            "median_of_task_median_paired_differences",
        )

    def test_gateway_provider_failure_stays_in_attempted_denominator(self):
        rows = self.complete_rows()
        failed = rows[-1]
        failed["result"].update(
            solved=False,
            checker_score=0.0,
            available=False,
            duration_s=30.0,
            failure_origin="gateway",
        )
        failed["proxy_metrics"]["calls"] = []

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)

        gateway = report["arms"]["gateway"]
        self.assertEqual(report["blocks"]["included"], 4)
        self.assertEqual(gateway["attempted_cells"], 4)
        self.assertEqual(gateway["metrics"]["availability"]["estimate"], 0.75)
        self.assertEqual(gateway["metrics"]["latency_s"]["estimate"], 22.5)

    def test_only_latest_block_attempt_is_reported(self):
        rows = [
            make_row(
                task="task_a", arm_id=arm, role=role, baseline=baseline,
                block_attempt=attempt, solved=attempt == 0,
                score=1.0 if attempt == 0 else 0.0,
                calls=[call(),],
            )
            for attempt in (0, 1)
            for arm, role, baseline in (
                ("direct", "direct", True),
                ("gateway", "gateway", False),
            )
        ]
        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        self.assertEqual(report["blocks"]["observed"], 1)
        self.assertEqual(report["arms"]["direct"]["attempted_cells"], 1)
        self.assertEqual(
            report["arms"]["direct"]["metrics"]["solve_rate"]["estimate"],
            0.0,
        )

    def test_invalid_and_incomplete_blocks_are_excluded_and_counted(self):
        rows = self.complete_rows()
        rows[0]["result"]["infrastructure_invalid_reason"] = "host"
        rows[2]["route_integrity"] = {
            "pass": False,
            "reasons": ["served_model_mismatch"],
        }
        rows.pop()

        report = gateway_report.aggregate(
            rows,
            expected_arm_ids=("direct", "gateway"),
            bootstrap_replicates=20,
        )

        self.assertEqual(report["blocks"]["observed"], 4)
        self.assertEqual(report["blocks"]["included"], 1)
        self.assertEqual(report["blocks"]["excluded_by_reason"], {
            "incomplete_all_arm_block": 1,
            "infrastructure:host": 1,
            "route_integrity:served_model_mismatch": 1,
        })
        self.assertEqual(report["arms"]["direct"]["attempted_cells"], 1)
        self.assertEqual(report["arms"]["gateway"]["attempted_cells"], 1)

    def test_cost_per_solve_requires_complete_call_coverage(self):
        rows = self.complete_rows()
        rows[-1]["proxy_metrics"]["calls"][0]["costs"].pop("gateway_reported")

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        cost = report["arms"]["gateway"]["costs"]["gateway_reported"]

        self.assertEqual(cost["basis_coverage"]["covered_calls"], 3)
        self.assertEqual(cost["basis_coverage"]["total_calls"], 4)
        self.assertFalse(cost["basis_coverage"]["complete"])
        self.assertIsNotNone(cost["attempted_cost_usd"]["estimate"])
        self.assertIsNone(cost["cost_per_solve_usd"])
        estimate = report["arms"]["gateway"]["costs"]["frozen_list_estimate"]
        self.assertEqual(estimate["cost_per_solve_usd"], 0.24)

    def test_gateway_reported_cost_has_complete_coverage_and_cost_per_solve(self):
        rows = self.complete_rows()
        for row in rows:
            if row["arm_role"] != "gateway":
                continue
            row["proxy_metrics"]["calls"][0]["costs"]["gateway_reported"] = {
                "amount_usd": 0.025,
                "currency": "USD",
                "effective_at": "2026-07-22T12:34:56Z",
            }

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        cost = report["arms"]["gateway"]["costs"]["gateway_reported"]

        self.assertEqual(cost["basis_coverage"]["covered_calls"], 4)
        self.assertEqual(cost["basis_coverage"]["total_calls"], 4)
        self.assertEqual(cost["basis_coverage"]["ratio"], 1.0)
        self.assertTrue(cost["basis_coverage"]["complete"])
        self.assertEqual(cost["attempted_cost_usd"]["estimate"], 0.025)
        self.assertEqual(cost["cost_per_solve_usd"], 0.05)
        self.assertEqual(cost["effective_at"], ["2026-07-22T12:34:56Z"])

    def test_route_distribution_is_task_weighted(self):
        rows = self.complete_rows()
        rows[1]["proxy_metrics"]["calls"].append(
            call(provider="Fallback", costs=False)
        )
        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        distribution = report["arms"]["gateway"]["route_distribution"]

        self.assertAlmostEqual(
            distribution["OpenRouter/gpt-fixed"]["share"], 5 / 6
        )
        self.assertAlmostEqual(
            distribution["Fallback/gpt-fixed"]["share"], 1 / 6
        )

    def test_route_label_does_not_duplicate_provider_prefix(self):
        self.assertEqual(
            gateway_report._route_label("OpenAI", "openai/gpt-fixed"),
            "openai/gpt-fixed",
        )

    def test_timeout_caps_end_to_end_latency(self):
        rows = self.complete_rows()
        rows[1]["result"]["duration_s"] = 300
        report = gateway_report.aggregate(rows, bootstrap_replicates=20)

        self.assertEqual(
            report["arms"]["gateway"]["metrics"]["latency_s"]["estimate"], 26.25
        )

    def test_timeout_without_observed_duration_uses_timeout_cap(self):
        rows = self.complete_rows()
        rows[1]["result"]["duration_s"] = None
        rows[1]["result"]["timed_out"] = True
        report = gateway_report.aggregate(rows, bootstrap_replicates=20)

        self.assertEqual(
            report["arms"]["gateway"]["metrics"]["latency_s"]["estimate"], 26.25
        )

    def test_conditional_contrasts_pair_within_blocks_before_tasks(self):
        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 1
        rows[1]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = None
        rows[2]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 3
        rows[3]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 5

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)

        # task_a contributes the paired block delta 5 - 3 = 2. task_b
        # contributes 2 - 1 = 1, so tasks equally produce 1.5.
        contrast = report["paired_contrasts"]["gateway"]["metrics"]["ttfb_s"]
        self.assertEqual(contrast["estimate"], 1.5)
        self.assertEqual(contrast["paired_task_coverage"]["covered"], 2)
        self.assertEqual(contrast["paired_block_coverage"]["covered"], 3)
        coverage = report["arms"]["gateway"]["metrics"]["ttfb_s"]["call_coverage"]
        self.assertEqual(coverage, {"covered": 3, "total": 4, "ratio": 0.75})

    def test_cache_metrics_are_task_weighted_with_paired_contrasts(self):
        rows = self.complete_rows()
        gateway_values = {
            ("task_a", 1): (20, 10),
            ("task_a", 2): (40, 20),
            ("task_b", 1): (0, 30),
            ("task_b", 2): (80, 40),
        }
        for row in rows:
            task = row["identity"]["task"]["name"]
            repetition = row["identity"]["schedule"]["repetition"]
            cached, written = (
                (0, 0)
                if row["arm_role"] == "direct"
                else gateway_values[(task, repetition)]
            )
            row["proxy_metrics"]["calls"][0]["cache"] = {
                "cached_input_tokens": cached,
                "cache_write_input_tokens": written,
            }

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        metrics = report["arms"]["gateway"]["metrics"]

        self.assertEqual(
            metrics["mean_cached_input_tokens_per_call"]["estimate"], 35.0
        )
        self.assertEqual(
            metrics["mean_cache_write_input_tokens_per_call"]["estimate"], 25.0
        )
        self.assertEqual(metrics["cache_hit_call_rate"]["estimate"], 0.75)
        self.assertAlmostEqual(metrics["cached_input_fraction"]["estimate"], 0.35)
        self.assertEqual(
            report["arms"]["direct"]["metrics"]["cache_hit_call_rate"]["estimate"],
            0.0,
        )
        for name in (
            "mean_cached_input_tokens_per_call",
            "mean_cache_write_input_tokens_per_call",
        ):
            self.assertEqual(
                metrics[name]["call_coverage"],
                {"covered": 4, "total": 4, "ratio": 1.0},
            )
            self.assertEqual(
                metrics[name]["cell_coverage"],
                {"covered": 4, "total": 4, "ratio": 1.0},
            )
            self.assertEqual(
                metrics[name]["task_coverage"],
                {"covered": 2, "total": 2, "ratio": 1.0},
            )

        contrasts = report["paired_contrasts"]["gateway"]["metrics"]
        self.assertEqual(
            contrasts["mean_cached_input_tokens_per_call"]["estimate"], 35.0
        )
        self.assertEqual(contrasts["cache_hit_call_rate"]["estimate"], 0.75)
        self.assertEqual(
            contrasts["mean_cache_write_input_tokens_per_call"]["estimate"], 25.0
        )

    def test_missing_cache_values_reduce_coverage_without_becoming_zero(self):
        rows = self.complete_rows()
        for row in rows:
            row["proxy_metrics"]["calls"][0]["cache"] = {
                "cached_input_tokens": 10,
                "cache_write_input_tokens": 20,
            }
        gateway_rows = [row for row in rows if row["arm_role"] == "gateway"]
        gateway_rows[0]["proxy_metrics"]["calls"][0].pop("cache")
        gateway_rows[1]["proxy_metrics"]["calls"][0]["cache"].pop(
            "cache_write_input_tokens"
        )

        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        metrics = report["arms"]["gateway"]["metrics"]
        cached = metrics["mean_cached_input_tokens_per_call"]
        written = metrics["mean_cache_write_input_tokens_per_call"]

        self.assertEqual(cached["estimate"], 10.0)
        self.assertEqual(
            cached["call_coverage"], {"covered": 3, "total": 4, "ratio": 0.75}
        )
        self.assertEqual(
            cached["cell_coverage"], {"covered": 3, "total": 4, "ratio": 0.75}
        )
        self.assertEqual(
            cached["task_coverage"], {"covered": 2, "total": 2, "ratio": 1.0}
        )
        self.assertEqual(written["estimate"], 20.0)
        self.assertEqual(
            written["call_coverage"], {"covered": 2, "total": 4, "ratio": 0.5}
        )
        self.assertEqual(
            written["cell_coverage"], {"covered": 2, "total": 4, "ratio": 0.5}
        )
        self.assertEqual(
            written["task_coverage"], {"covered": 1, "total": 2, "ratio": 0.5}
        )
        contrast = report["paired_contrasts"]["gateway"]["metrics"][
            "mean_cache_write_input_tokens_per_call"
        ]
        self.assertEqual(contrast["paired_block_coverage"]["covered"], 2)
        self.assertEqual(contrast["paired_task_coverage"]["covered"], 1)

    def test_bootstrap_and_json_are_deterministic_and_safe(self):
        first = gateway_report.aggregate(
            self.complete_rows(), bootstrap_replicates=100, bootstrap_seed=99
        )
        second = gateway_report.aggregate(
            reversed(self.complete_rows()), bootstrap_replicates=100, bootstrap_seed=99
        )

        self.assertEqual(first, second)
        encoded = json.dumps(first, allow_nan=False, sort_keys=True)
        self.assertIn('"task_cluster_bootstrap_percentile"', encoded)

    def test_text_renderer_is_concise_and_names_exclusions(self):
        rows = self.complete_rows()
        rows[0]["route_integrity"] = {"pass": False, "reasons": ["ledger_gap"]}
        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        text = gateway_report.render_text(report)

        self.assertIn("Gateway Bench: fixed_model_provider", text)
        self.assertIn("route_integrity:ledger_gap=1", text)
        self.assertIn("gateway - direct", text)
        self.assertLessEqual(len(text.splitlines()), 12)

    def test_rejects_mixed_experiment_track_or_stratum(self):
        mutators = (
            lambda row: row["identity"]["experiment"].update(digest="f" * 64),
            lambda row: row["identity"]["benchmark"].update(track="provider_router"),
            lambda row: row["identity"]["comparison"].update(policy_digest="f" * 64),
        )
        for mutate in mutators:
            rows = self.complete_rows()
            mutate(rows[-1])
            identity = results.validate_gateway_identity(rows[-1]["identity"])
            rows[-1]["run_id"] = results.make_gateway_run_id(identity)
            rows[-1]["cell_id"] = results.make_gateway_cell_id(identity)
            with self.subTest(mutate=mutate), self.assertRaises(
                gateway_report.GatewayReportError
            ):
                gateway_report.aggregate(rows, bootstrap_replicates=10)

    def test_rejects_duplicate_cells_and_inconsistent_arm_metadata(self):
        rows = self.complete_rows()
        with self.assertRaisesRegex(gateway_report.GatewayReportError, "duplicate cell_id"):
            gateway_report.aggregate(rows + [copy.deepcopy(rows[0])], bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[-1]["baseline"] = True
        with self.assertRaisesRegex(gateway_report.GatewayReportError, "metadata"):
            gateway_report.aggregate(rows, bootstrap_replicates=10)

    def test_rejects_malformed_metric_and_route_evidence(self):
        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["generation"].pop("duration_s")
        with self.assertRaisesRegex(gateway_report.GatewayReportError, "must pair"):
            gateway_report.aggregate(rows, bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[0]["route_integrity"] = {"pass": True, "reasons": ["contradiction"]}
        with self.assertRaisesRegex(gateway_report.GatewayReportError, "passes"):
            gateway_report.aggregate(rows, bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["costs"]["gateway_reported"][
            "currency"
        ] = "EUR"
        with self.assertRaisesRegex(gateway_report.GatewayReportError, "must be USD"):
            gateway_report.aggregate(rows, bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["cache"] = {
            "cached_input_tokens": -1,
            "cache_write_input_tokens": 0,
        }
        with self.assertRaisesRegex(
            gateway_report.GatewayReportError, "cached_input_tokens"
        ):
            gateway_report.aggregate(rows, bootstrap_replicates=10)


if __name__ == "__main__":
    unittest.main()
