import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench import (
    gateway_probe_http,
    gateway_probe_report,
    gateway_probe_results,
    gateway_probe_run,
    gateway_probe_spec,
    gateway_run,
    gateway_spec,
)
from obench.gateway_probe_models import GatewayProbeRunError
from obench.tests.test_gateway_probe_results import bound_row
from obench.tests.test_gateway_probe_spec import manifest


def environment():
    return {
        "OPENAI_API_KEY": "direct-secret",
        "OPENROUTER_API_KEY": "gateway-secret",
        gateway_run.FROZEN_PRICES_ENV: json.dumps({
            "openai/gpt-4o-mini": {
                "input_per_million": "1",
                "output_per_million": "2",
                "effective_at": "2026-07-25T00:00:00Z",
            }
        }),
    }


def canned_result(*, stop_required=False, condition="cold"):
    amount = "1.00" if stop_required else "0"
    attempt_outcome = {
        "success": True,
        "http_status": 200,
        "timed_out": False,
        "error_class": None,
        "error_detail": None,
        "semantic_output_started": True,
    }
    result = {
        "outcome": {
            "attempted": True,
            "success": True,
            "available": True,
            "http_status": 200,
            "timed_out": False,
            "error_class": None,
            "error_detail": None,
            "budget_exhausted_reason": (
                "usd_cap_reached" if stop_required else None
            ),
        },
        "route_integrity": {
            "status": "verified",
            "pass": True,
            "reasons": [],
        },
        "request_metrics": {
            "setup": {
                "dns_s": 0.01,
                "tcp_s": 0.01,
                "tls_s": 0.01,
            },
            "timing": {
                "request_to_response_headers_s": 0.1,
                "request_to_first_body_byte_s": 0.15,
                "request_to_semantic_ttft_s": 0.2,
                "request_stream_total_s": 0.3,
                "cold_end_to_end_response_headers_s": 0.13,
                "cold_end_to_end_first_body_byte_s": 0.18,
                "cold_end_to_end_semantic_ttft_s": 0.23,
                "cold_end_to_end_stream_total_s": 0.33,
            },
            "receipt_headers": {},
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
            "generation": {"tokens_per_second": 10.0},
            "cache": {
                "cached_input_tokens": None,
                "cache_write_input_tokens": None,
            },
            "route": {
                "served_model": "gpt-4o-mini",
                "provider": "openai",
            },
            "costs": {},
            "stream": {
                "done": True,
                "terminal_status": "completed",
                "finalized": True,
            },
            "coverage": {},
        },
        "reuse_evidence": {
            "required": False,
            "completed": False,
            "http_status": None,
            "socket_reused": None,
            "primer_nonce_sha256": "1" * 64,
            "measured_nonce_sha256": "2" * 64,
            "setup": {"dns_s": None, "tcp_s": None, "tls_s": None},
            "receipt_headers": {},
            "route_integrity": None,
            "usage": None,
            "cache": None,
            "costs": {},
        },
        "billing": {
            "primer_cost_usd": None,
            "measured_cost_usd": amount,
            "charged_cost_usd": amount,
            "observed_cost_usd": amount,
            "known_observed_cost_usd": amount,
            "budget_debit_usd": amount,
            "cost_status": "observed",
            "unknown_cost_attempts": 0,
            "stop_required": stop_required,
        },
        "retry_evidence": {
            "max_total_attempts": 1,
            "max_input_tokens": None,
            "max_output_tokens": 64,
            "retry_deadline_s": None,
            "attempt_count": 1,
            "recovered": False,
            "first_attempt_outcome": dict(attempt_outcome),
            "eventual_outcome": dict(attempt_outcome),
            "recovery_timing": {
                "initial_request_to_final_response_headers_s": 0.1,
                "initial_request_to_final_semantic_output_s": 0.2,
                "initial_request_to_completion_s": 0.3,
                "final_attempt_request_start_offset_s": 0.0,
            },
            "attempts": [{
                "attempt_number": 1,
                "phase": "measured",
                "outcome": dict(attempt_outcome),
                "timing": {
                    "initial_request_start_offset_s": 0.0,
                    "request_to_response_headers_s": 0.1,
                    "request_to_semantic_output_s": 0.2,
                    "attempt_total_s": 0.3,
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
                    "primer_cost_usd": None,
                    "measured_cost_usd": amount,
                    "observed_cost_usd": amount,
                    "known_observed_cost_usd": amount,
                    "budget_debit_usd": amount,
                    "reservation_usd": "0",
                    "cost_status": "observed",
                },
            }],
        },
    }
    if condition == "warm":
        result["request_metrics"]["setup"] = None
        for name in (
            "cold_end_to_end_response_headers_s",
            "cold_end_to_end_first_body_byte_s",
            "cold_end_to_end_semantic_ttft_s",
            "cold_end_to_end_stream_total_s",
        ):
            result["request_metrics"]["timing"][name] = None
        result["reuse_evidence"]["required"] = True
        result["reuse_evidence"]["completed"] = True
        result["reuse_evidence"]["socket_reused"] = True
        result["reuse_evidence"]["http_status"] = 200
        result["reuse_evidence"]["route_integrity"] = {
            "status": "verified",
            "pass": True,
            "reasons": [],
        }
    return result


def canned_execute(*, stop_required=False):
    def execute(**kwargs):
        return canned_result(
            stop_required=stop_required,
            condition=kwargs["block"].condition,
        )
    return execute


def cost_unavailable_result(*, condition, reason="measured_cost_unavailable"):
    result = canned_result(condition=condition)
    result["outcome"].update({
        "success": False,
        "available": False,
        "http_status": 429,
        "error_class": "http",
        "error_detail": "http_status",
        "budget_exhausted_reason": reason,
    })
    result["billing"].update({
        "measured_cost_usd": None,
        "charged_cost_usd": None,
        "observed_cost_usd": None,
        "known_observed_cost_usd": "0",
        "budget_debit_usd": "0.001",
        "cost_status": "reserved_unknown",
        "unknown_cost_attempts": 1,
        "stop_required": True,
    })
    attempt_outcome = {
        "success": False,
        "http_status": 429,
        "timed_out": False,
        "error_class": "http",
        "error_detail": "http_status",
        "semantic_output_started": False,
    }
    result["retry_evidence"].update({
        "first_attempt_outcome": dict(attempt_outcome),
        "eventual_outcome": dict(attempt_outcome),
    })
    attempt = result["retry_evidence"]["attempts"][0]
    attempt["outcome"] = dict(attempt_outcome)
    attempt["retry"].update({
        "eligible": True,
        "not_retried_reason": "attempt_limit",
    })
    attempt["cost"].update({
        "measured_cost_usd": None,
        "observed_cost_usd": None,
        "known_observed_cost_usd": "0",
        "budget_debit_usd": "0.001",
        "reservation_usd": "0.001",
        "cost_status": "reserved_unknown",
    })
    return result


def mark_cost_unavailable(row, *, reason="measured_cost_unavailable"):
    result = cost_unavailable_result(
        condition=row["identity"]["schedule"]["condition"],
        reason=reason,
    )
    row["outcome"] = result["outcome"]
    row["billing"] = result["billing"]
    row["retry_evidence"] = result["retry_evidence"]


def write_rows(path, rows):
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class GatewayProbeRunTests(unittest.TestCase):
    def test_schedule_is_deterministic_and_conditions_are_interleaved(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            spec_path.write_text(manifest(), encoding="utf-8")
            base = gateway_probe_spec.load_experiment(spec_path)
        expanded = dataclasses.replace(base, repetitions=4)
        first = gateway_probe_run.build_schedule(expanded)
        self.assertEqual(first, gateway_probe_run.build_schedule(expanded))
        self.assertEqual(
            [item.condition for item in first],
            [
                first[0].condition,
                "warm" if first[0].condition == "cold" else "cold",
            ]
            * 4,
        )

    def test_resume_validation_precedes_budget_accounting(self):
        env = environment()
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            experiment = gateway_probe_spec.load_experiment(spec_path)
            schedule = gateway_probe_run.build_schedule(experiment)
            schedule_digest = gateway_spec.canonical_digest(
                [dataclasses.asdict(block) for block in schedule]
            )
            _prices, price_snapshot = gateway_run.load_frozen_prices(env)
            price_digest = gateway_spec.canonical_digest(
                price_snapshot
            )
            row = bound_row(
                experiment, schedule[0], schedule_digest, price_digest
            )
            row["cell_id"] = "forged"
            results_path.write_text(
                json.dumps(row, sort_keys=True) + "\n", encoding="utf-8"
            )
            with mock.patch.object(
                gateway_probe_results,
                "charged_cost",
                side_effect=AssertionError(
                    "budget read before identity validation"
                ),
            ):
                with self.assertRaises(GatewayProbeRunError):
                    gateway_probe_run.run_experiment(
                        spec_path,
                        results_path=results_path,
                        environ=env,
                        allow_cost_unavailable_block_recovery=True,
                    )

    def test_cumulative_cap_stops_after_paid_partial_block_and_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                side_effect=canned_execute(stop_required=True),
            ) as execute:
                first = gateway_probe_run.run_experiment(
                    spec_path, results_path=results_path, environ=environment()
                )
                second = gateway_probe_run.run_experiment(
                    spec_path, results_path=results_path, environ=environment()
                )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(first.rows_appended, 1)
        self.assertEqual(first.blocks_completed, 0)
        self.assertEqual(second.rows_appended, 0)

    def test_complete_cost_unavailable_block_requires_explicit_recovery_and_stays_in_report(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                side_effect=canned_execute(),
            ):
                gateway_probe_run.run_experiment(
                    spec_path,
                    results_path=results_path,
                    environ=environment(),
                )
            historical = gateway_probe_results.load_results(results_path)[:2]
            mark_cost_unavailable(historical[-1])
            write_rows(results_path, historical)
            historical_bytes = results_path.read_bytes()
            stopped_arm = historical[-1]["identity"]["arm"]["id"]
            stopped_condition = historical[-1]["identity"]["schedule"]["condition"]

            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                side_effect=canned_execute(),
            ) as execute:
                refused = gateway_probe_run.run_experiment(
                    spec_path,
                    results_path=results_path,
                    environ=environment(),
                )
                recovered = gateway_probe_run.run_experiment(
                    spec_path,
                    results_path=results_path,
                    environ=environment(),
                    allow_cost_unavailable_block_recovery=True,
                )

            final_rows = gateway_probe_results.load_results(results_path)
            report = gateway_probe_report.aggregate(final_rows)
            final_bytes = results_path.read_bytes()
        self.assertEqual(refused.rows_appended, 0)
        self.assertEqual(execute.call_count, 6)
        self.assertEqual(recovered.rows_appended, 6)
        self.assertEqual(recovered.blocks_completed, 3)
        self.assertEqual(recovered.blocks_skipped, 1)
        self.assertTrue(final_bytes.startswith(historical_bytes))
        self.assertEqual(final_rows[:2], historical)
        self.assertEqual(report["complete_blocks"], {"cold": 2, "warm": 2})
        availability = report["arms"][stopped_arm]["conditions"][
            stopped_condition
        ]["availability"]
        self.assertEqual(availability["attempted"], 2)
        self.assertEqual(availability["successes"], 1)

    def test_cost_unavailable_recovery_refuses_incomplete_historical_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                side_effect=canned_execute(),
            ):
                gateway_probe_run.run_experiment(
                    spec_path,
                    results_path=results_path,
                    environ=environment(),
                )
            historical = gateway_probe_results.load_results(results_path)[:1]
            mark_cost_unavailable(historical[0])
            write_rows(results_path, historical)
            with mock.patch.object(
                gateway_probe_http, "execute_request"
            ) as execute:
                with self.assertRaisesRegex(
                    GatewayProbeRunError,
                    "latest complete expected-arm block attempt",
                ):
                    gateway_probe_run.run_experiment(
                        spec_path,
                        results_path=results_path,
                        environ=environment(),
                        allow_cost_unavailable_block_recovery=True,
                    )
        execute.assert_not_called()

    def test_cost_unavailable_recovery_refuses_cap_stops_and_known_cap(self):
        for reason, exhaust_known_cap, stop_required, expected in (
            (
                "usd_cap_reached",
                False,
                True,
                "cannot bypass stop reason",
            ),
            (
                "usd_cap_reached_by_primer",
                False,
                True,
                "cannot bypass stop reason",
            ),
            (
                "usd_cap_reached",
                False,
                False,
                "budget stop evidence is inconsistent",
            ),
            (
                "measured_cost_unavailable",
                True,
                True,
                "budget debit below budget.usd_cap",
            ),
        ):
            with self.subTest(
                reason=reason,
                exhaust_known_cap=exhaust_known_cap,
                stop_required=stop_required,
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    spec_path = Path(tmp, "probe.toml")
                    results_path = Path(tmp, "probe.jsonl")
                    spec_path.write_text(manifest(), encoding="utf-8")
                    with mock.patch.object(
                        gateway_probe_http,
                        "execute_request",
                        side_effect=canned_execute(),
                    ):
                        gateway_probe_run.run_experiment(
                            spec_path,
                            results_path=results_path,
                            environ=environment(),
                        )
                    historical = gateway_probe_results.load_results(
                        results_path
                    )[:2]
                    mark_cost_unavailable(historical[-1], reason=reason)
                    historical[-1]["billing"]["stop_required"] = stop_required
                    if exhaust_known_cap:
                        historical[-1]["billing"].update({
                            "primer_cost_usd": "0.05",
                            "known_observed_cost_usd": "0.05",
                            "budget_debit_usd": "0.05",
                        })
                        historical[-1]["retry_evidence"]["attempts"][0][
                            "cost"
                        ].update({
                            "primer_cost_usd": "0.05",
                            "known_observed_cost_usd": "0.05",
                            "budget_debit_usd": "0.05",
                            "reservation_usd": "0.05",
                        })
                    write_rows(results_path, historical)
                    with mock.patch.object(
                        gateway_probe_http, "execute_request"
                    ) as execute:
                        with self.assertRaisesRegex(
                            GatewayProbeRunError, expected
                        ):
                            gateway_probe_run.run_experiment(
                                spec_path,
                                results_path=results_path,
                                environ=environment(),
                                allow_cost_unavailable_block_recovery=True,
                            )
                execute.assert_not_called()

    def test_new_cost_unavailable_recovery_finishes_current_block_only(self):
        spec_path = (
            Path(__file__).parents[1]
            / "examples"
            / "gateway-probe-five-way-responses.toml"
        )
        env = environment()
        env.update({
            "CLOUDFLARE_API_TOKEN": "cloudflare-secret",
            "VERCEL_API_KEY": "vercel-secret",
            "CONCENTRATE_API_KEY": "concentrate-secret",
        })
        summaries = {}
        rows_by_mode = {}
        for recovery_enabled in (False, True):
            with self.subTest(recovery_enabled=recovery_enabled):
                with tempfile.TemporaryDirectory() as tmp:
                    results_path = Path(tmp, "probe.jsonl")
                    call_count = 0

                    def execute(**kwargs):
                        nonlocal call_count
                        call_count += 1
                        if call_count == 1:
                            return cost_unavailable_result(
                                condition=kwargs["block"].condition
                            )
                        return canned_result(
                            condition=kwargs["block"].condition
                        )

                    with mock.patch.object(
                        gateway_probe_http,
                        "execute_request",
                        side_effect=execute,
                    ):
                        summaries[recovery_enabled] = (
                            gateway_probe_run.run_experiment(
                                spec_path,
                                results_path=results_path,
                                environ=env,
                                allow_cost_unavailable_block_recovery=(
                                    recovery_enabled
                                ),
                            )
                        )
                    rows_by_mode[recovery_enabled] = (
                        gateway_probe_results.load_results(results_path)
                    )

        self.assertEqual(summaries[False].rows_appended, 1)
        self.assertEqual(summaries[False].blocks_completed, 0)
        self.assertEqual(summaries[True].rows_appended, 5)
        self.assertEqual(summaries[True].blocks_completed, 1)
        recovered_rows = rows_by_mode[True]
        self.assertEqual(
            {row["identity"]["schedule"]["block_id"] for row in recovered_rows},
            {recovered_rows[0]["identity"]["schedule"]["block_id"]},
        )
        self.assertEqual(
            {row["identity"]["arm"]["id"] for row in recovered_rows},
            {
                "direct-openai",
                "cloudflare-openai",
                "openrouter-openai",
                "vercel-openai",
                "concentrate-openai",
            },
        )
        self.assertEqual(
            recovered_rows[0]["outcome"]["budget_exhausted_reason"],
            "measured_cost_unavailable",
        )
        self.assertFalse(recovered_rows[0]["outcome"]["available"])

    def test_run_resumes_complete_blocks_and_replaces_partial_latest_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                side_effect=canned_execute(),
            ):
                first = gateway_probe_run.run_experiment(
                    spec_path, results_path=results_path, environ=environment()
                )
                second = gateway_probe_run.run_experiment(
                    spec_path, results_path=results_path, environ=environment()
                )
                lines = results_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                results_path.write_text(
                    "\n".join(lines[:-1]) + "\n", encoding="utf-8"
                )
                repaired = gateway_probe_run.run_experiment(
                    spec_path, results_path=results_path, environ=environment()
                )
        self.assertEqual(first.rows_appended, 8)
        self.assertEqual(second.rows_appended, 0)
        self.assertEqual(second.blocks_skipped, 4)
        self.assertEqual(repaired.rows_appended, 2)
        self.assertEqual(repaired.blocks_replaced, 1)


if __name__ == "__main__":
    unittest.main()
