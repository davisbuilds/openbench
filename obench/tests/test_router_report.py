"""Contract tests for the schema-v2 Gateway Tax report."""

import copy
import json
import unittest

from obench import results, router_report


DIGESTS = {
    name: format(index, "064x")
    for index, name in enumerate(
        (
            "experiment",
            "direct_arm",
            "gateway_arm",
            "auto_arm",
            "fixed_arm",
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
    route_pass=True,
    route_reasons=None,
    track="gateway_tax",
    router_mode=None,
    model_match="exact_revision",
):
    block_id = f"{task}-{window}-{repetition}-a{block_attempt}"
    identity = results.CellIdentity.for_router(
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
    }
    row = {
        "schema_version": 2,
        "benchmark": "router",
        "run_id": results.make_router_run_id(identity),
        "cell_id": results.make_router_cell_id(identity),
        "identity": identity.as_dict(),
        "arm_role": role,
        "model_match": model_match,
        "baseline": baseline,
        "result": result,
        "route_integrity": {
            "pass": route_pass,
            "reasons": route_reasons or [],
        },
        "proxy_metrics": {"calls": [] if calls is None else calls},
    }
    if router_mode is not None:
        row["router_mode"] = router_mode
    return row


def call(
    *,
    provider="OpenAI",
    model="gpt-fixed",
    ttfb=1.0,
    ttft=2.0,
    tokens=10,
    generation_s=2.0,
    costs=True,
    attempts=None,
    attempts_present=False,
    cache=None,
):
    bases = {}
    if costs:
        bases = {
            "router_reported": {
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


class RouterReportTests(unittest.TestCase):
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
        report = router_report.aggregate(
            self.complete_rows(), bootstrap_replicates=200, bootstrap_seed=7
        )
        direct = report["arms"]["direct"]
        gateway = report["arms"]["gateway"]

        self.assertEqual(report["blocks"], {
            "observed": 4,
            "included": 4,
            "excluded": 0,
            "excluded_by_reason": {},
        })
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

    def test_model_router_reports_auto_minus_fixed_and_attempt_coverage(self):
        rows = []
        for task in ("task_a", "task_b"):
            rows.extend([
                make_row(
                    task=task,
                    arm_id="fixed",
                    role="gateway",
                    router_mode="fixed",
                    baseline=True,
                    track="model_router",
                    calls=[call(
                        provider="OpenAI",
                        model="openai/gpt-fixed",
                    )],
                ),
                make_row(
                    task=task,
                    arm_id="auto",
                    role="gateway",
                    router_mode="auto",
                    baseline=False,
                    track="model_router",
                    solved=task == "task_a",
                    score=1.0 if task == "task_a" else 0.0,
                    calls=[call(
                        provider="Anthropic",
                        model="anthropic/claude-routed",
                        attempts=[
                            {
                                "provider": "OpenAI",
                                "model": "openai/gpt-fixed",
                                "status": 429,
                            },
                            {
                                "provider": "Anthropic",
                                "model": "anthropic/claude-routed",
                                "status": 200,
                            },
                        ],
                        attempts_present=True,
                    )],
                ),
            ])

        report = router_report.aggregate(
            rows, bootstrap_replicates=50, bootstrap_seed=9
        )

        self.assertEqual(report["track"], "model_router")
        self.assertEqual(report["model_match"], "exact_revision")
        self.assertEqual(report["baseline_arm"], "fixed")
        self.assertEqual(
            report["paired_contrasts"]["auto"]["direction"],
            "auto_minus_fixed",
        )
        self.assertNotIn("fixed", report["paired_contrasts"])
        self.assertEqual(
            report["arms"]["auto"]["routing"]["fallback_call_rate"]["estimate"],
            1.0,
        )
        self.assertEqual(
            report["arms"]["auto"]["routing"]["mean_attempts_per_call"]["estimate"],
            2.0,
        )
        self.assertEqual(
            report["arms"]["auto"]["primary_cost_basis"], "router_reported"
        )
        self.assertEqual(
            report["arms"]["auto"]["actual_cost"]["cost_per_solve_usd"], 0.2
        )
        rendered = router_report.render_text(report)
        self.assertIn("auto_minus_fixed", rendered)
        self.assertNotIn("gateway tax", rendered.lower())

    def test_router_provider_failure_stays_in_attempted_denominator(self):
        rows = self.complete_rows()
        failed = rows[-1]
        failed["result"].update(
            solved=False,
            checker_score=0.0,
            available=False,
            duration_s=30.0,
            failure_origin="router",
        )
        failed["proxy_metrics"]["calls"] = []

        report = router_report.aggregate(rows, bootstrap_replicates=20)

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
        report = router_report.aggregate(rows, bootstrap_replicates=20)
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

        report = router_report.aggregate(
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
        rows[-1]["proxy_metrics"]["calls"][0]["costs"].pop("router_reported")

        report = router_report.aggregate(rows, bootstrap_replicates=20)
        cost = report["arms"]["gateway"]["costs"]["router_reported"]

        self.assertEqual(cost["basis_coverage"]["covered_calls"], 3)
        self.assertEqual(cost["basis_coverage"]["total_calls"], 4)
        self.assertFalse(cost["basis_coverage"]["complete"])
        self.assertIsNotNone(cost["attempted_cost_usd"]["estimate"])
        self.assertIsNone(cost["cost_per_solve_usd"])
        estimate = report["arms"]["gateway"]["costs"]["frozen_list_estimate"]
        self.assertEqual(estimate["cost_per_solve_usd"], 0.24)

    def test_router_reported_cost_has_complete_coverage_and_cost_per_solve(self):
        rows = self.complete_rows()
        for row in rows:
            if row["arm_role"] != "gateway":
                continue
            row["proxy_metrics"]["calls"][0]["costs"]["router_reported"] = {
                "amount_usd": 0.025,
                "currency": "USD",
                "effective_at": "2026-07-22T12:34:56Z",
            }

        report = router_report.aggregate(rows, bootstrap_replicates=20)
        cost = report["arms"]["gateway"]["costs"]["router_reported"]

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
        report = router_report.aggregate(rows, bootstrap_replicates=20)
        distribution = report["arms"]["gateway"]["route_distribution"]

        self.assertAlmostEqual(
            distribution["OpenRouter/gpt-fixed"]["share"], 5 / 6
        )
        self.assertAlmostEqual(
            distribution["Fallback/gpt-fixed"]["share"], 1 / 6
        )

    def test_route_label_does_not_duplicate_provider_prefix(self):
        self.assertEqual(
            router_report._route_label("OpenAI", "openai/gpt-fixed"),
            "openai/gpt-fixed",
        )

    def test_timeout_caps_end_to_end_latency(self):
        rows = self.complete_rows()
        rows[1]["result"]["duration_s"] = 300
        report = router_report.aggregate(rows, bootstrap_replicates=20)

        self.assertEqual(
            report["arms"]["gateway"]["metrics"]["latency_s"]["estimate"], 26.25
        )

    def test_timeout_without_observed_duration_uses_timeout_cap(self):
        rows = self.complete_rows()
        rows[1]["result"]["duration_s"] = None
        rows[1]["result"]["timed_out"] = True
        report = router_report.aggregate(rows, bootstrap_replicates=20)

        self.assertEqual(
            report["arms"]["gateway"]["metrics"]["latency_s"]["estimate"], 26.25
        )

    def test_conditional_contrasts_pair_within_blocks_before_tasks(self):
        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 1
        rows[1]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = None
        rows[2]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 3
        rows[3]["proxy_metrics"]["calls"][0]["timing"]["ttfb_s"] = 5

        report = router_report.aggregate(rows, bootstrap_replicates=20)

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

        report = router_report.aggregate(rows, bootstrap_replicates=20)
        metrics = report["arms"]["gateway"]["metrics"]

        self.assertEqual(
            metrics["mean_cached_input_tokens_per_call"]["estimate"], 35.0
        )
        self.assertEqual(metrics["cache_hit_call_rate"]["estimate"], 0.75)
        self.assertEqual(
            metrics["mean_cache_write_input_tokens_per_call"]["estimate"], 25.0
        )
        self.assertEqual(
            report["arms"]["direct"]["metrics"]["cache_hit_call_rate"]["estimate"],
            0.0,
        )
        for name in (
            "mean_cached_input_tokens_per_call",
            "cache_hit_call_rate",
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

        report = router_report.aggregate(rows, bootstrap_replicates=20)
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
        first = router_report.aggregate(
            self.complete_rows(), bootstrap_replicates=100, bootstrap_seed=99
        )
        second = router_report.aggregate(
            reversed(self.complete_rows()), bootstrap_replicates=100, bootstrap_seed=99
        )

        self.assertEqual(first, second)
        encoded = json.dumps(first, allow_nan=False, sort_keys=True)
        self.assertIn('"task_cluster_bootstrap_percentile"', encoded)

    def test_text_renderer_is_concise_and_names_exclusions(self):
        rows = self.complete_rows()
        rows[0]["route_integrity"] = {"pass": False, "reasons": ["ledger_gap"]}
        report = router_report.aggregate(rows, bootstrap_replicates=20)
        text = router_report.render_text(report)

        self.assertIn("Router Bench: gateway_tax", text)
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
            identity = results.validate_router_identity(rows[-1]["identity"])
            rows[-1]["run_id"] = results.make_router_run_id(identity)
            rows[-1]["cell_id"] = results.make_router_cell_id(identity)
            with self.subTest(mutate=mutate), self.assertRaises(
                router_report.RouterReportError
            ):
                router_report.aggregate(rows, bootstrap_replicates=10)

    def test_rejects_duplicate_cells_and_inconsistent_arm_metadata(self):
        rows = self.complete_rows()
        with self.assertRaisesRegex(router_report.RouterReportError, "duplicate cell_id"):
            router_report.aggregate(rows + [copy.deepcopy(rows[0])], bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[-1]["baseline"] = True
        with self.assertRaisesRegex(router_report.RouterReportError, "metadata"):
            router_report.aggregate(rows, bootstrap_replicates=10)

    def test_rejects_malformed_metric_and_route_evidence(self):
        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["generation"].pop("duration_s")
        with self.assertRaisesRegex(router_report.RouterReportError, "must pair"):
            router_report.aggregate(rows, bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[0]["route_integrity"] = {"pass": True, "reasons": ["contradiction"]}
        with self.assertRaisesRegex(router_report.RouterReportError, "passes"):
            router_report.aggregate(rows, bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["costs"]["router_reported"][
            "currency"
        ] = "EUR"
        with self.assertRaisesRegex(router_report.RouterReportError, "must be USD"):
            router_report.aggregate(rows, bootstrap_replicates=10)

        rows = self.complete_rows()
        rows[0]["proxy_metrics"]["calls"][0]["cache"] = {
            "cached_input_tokens": -1,
            "cache_write_input_tokens": 0,
        }
        with self.assertRaisesRegex(
            router_report.RouterReportError, "cached_input_tokens"
        ):
            router_report.aggregate(rows, bootstrap_replicates=10)


if __name__ == "__main__":
    unittest.main()
