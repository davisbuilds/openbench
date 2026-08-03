import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from obench import (
    gateway_probe_results,
    gateway_probe_run,
    gateway_probe_spec,
    gateway_run,
    gateway_spec,
)
from obench.gateway_probe_models import GatewayProbeRunError
from obench.tests.test_gateway_probe_spec import manifest


def bound_row(experiment, block, schedule_digest, price_digest):
    arm = next(
        item for item in experiment.arms if item.arm_id == block.arm_ids[0]
    )
    identity = gateway_probe_results.make_identity(
        experiment, arm, block, 0, schedule_digest, price_digest
    )
    cold = block.condition == "cold"
    return {
        "schema_version": gateway_probe_results.RESULT_SCHEMA_VERSION,
        "benchmark": gateway_probe_results.BENCHMARK,
        "cell_id": gateway_probe_results.cell_id(identity),
        "identity": identity,
        "expected_arm_ids": sorted(item.arm_id for item in experiment.arms),
        "scheduled_blocks_per_condition": (
            len(experiment.cases) * experiment.repetitions
        ),
        "arm_role": arm.route_kind,
        "baseline": arm.baseline,
        "model_match": experiment.model_match,
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
        "route_integrity": {
            "status": "verified",
            "pass": True,
            "reasons": [],
        },
        "request_metrics": {
            "setup": (
                {"dns_s": 0.01, "tcp_s": 0.02, "tls_s": 0.03}
                if cold else None
            ),
            "timing": {
                "request_to_response_headers_s": 0.1,
                "request_to_first_body_byte_s": 0.15,
                "request_to_semantic_ttft_s": 0.2,
                "request_stream_total_s": 0.3,
                "cold_end_to_end_response_headers_s": 0.16 if cold else None,
                "cold_end_to_end_first_body_byte_s": 0.21 if cold else None,
                "cold_end_to_end_semantic_ttft_s": 0.26 if cold else None,
                "cold_end_to_end_stream_total_s": 0.36 if cold else None,
            },
            "receipt_headers": {},
            "usage": {
                "input_tokens": 5,
                "output_tokens": 4,
                "total_tokens": 9,
                "output_tokens_details": {
                    "reasoning_tokens": 3,
                    "text_tokens": 1,
                },
            },
            "generation": None,
            "cache": {
                "cached_input_tokens": None,
                "cache_write_input_tokens": None,
            },
            "route": None,
            "costs": {},
            "stream": {
                "done": True,
                "terminal_status": "completed",
                "finish_reason": "length",
                "finalized": True,
            },
            "coverage": None,
        },
        "reuse_evidence": {
            "required": not cold,
            "completed": not cold,
            "http_status": 200 if not cold else None,
            "socket_reused": True if not cold else None,
            "primer_nonce_sha256": "1" * 64,
            "measured_nonce_sha256": "2" * 64,
            "setup": {"dns_s": 0.01, "tcp_s": 0.02, "tls_s": 0.03},
            "receipt_headers": {},
            "route_integrity": (
                {"status": "verified", "pass": True, "reasons": []}
                if not cold else None
            ),
            "usage": None,
            "cache": None,
            "costs": {},
            "stream": (
                {
                    "done": True,
                    "terminal_status": "completed",
                    "finish_reason": "stop",
                    "finalized": True,
                }
                if not cold else None
            ),
        },
        "billing": {
            "primer_cost_usd": None,
            "measured_cost_usd": "0",
            "charged_cost_usd": "0",
            "observed_cost_usd": "0",
            "known_observed_cost_usd": "0",
            "budget_debit_usd": "0",
            "cost_status": "observed",
            "unknown_cost_attempts": 0,
            "stop_required": True,
        },
        "retry_evidence": {
            "max_total_attempts": 1,
            "max_input_tokens": None,
            "max_output_tokens": 64,
            "retry_deadline_s": None,
            "reservation_input_per_million_usd": "0",
            "reservation_output_per_million_usd": "0",
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
                "initial_request_to_final_response_headers_s": 0.1,
                "initial_request_to_final_semantic_output_s": 0.2,
                "initial_request_to_completion_s": 0.3,
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
                    "measured_cost_usd": "0",
                    "observed_cost_usd": "0",
                    "known_observed_cost_usd": "0",
                    "budget_debit_usd": "0",
                    "reservation_usd": "0",
                    "cost_status": "observed",
                },
            }],
        },
    }


class GatewayProbeResultsTests(unittest.TestCase):
    def test_schema_v5_and_recovered_primer_attempts_are_fail_closed(self):
        env = {
            gateway_run.FROZEN_PRICES_ENV: json.dumps({
                "openai/gpt-4o-mini": {
                    "input_per_million": "1",
                    "output_per_million": "2",
                    "effective_at": "2026-07-25T00:00:00Z",
                }
            }),
        }
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            spec_path.write_text(manifest(), encoding="utf-8")
            experiment = gateway_probe_spec.load_experiment(spec_path)
            schedule = gateway_probe_run.build_schedule(experiment)
            schedule_digest = gateway_spec.canonical_digest(
                [dataclasses.asdict(block) for block in schedule]
            )
            _prices, price_snapshot = gateway_run.load_frozen_prices(env)
            price_digest = gateway_spec.canonical_digest(price_snapshot)
            block = next(
                item for item in schedule if item.condition == "warm"
            )
            row = bound_row(
                experiment, block, schedule_digest, price_digest
            )
            row["reuse_evidence"]["route_integrity"] = {
                "status": "verified",
                "pass": True,
                "reasons": [
                    "multiple_attempts",
                    "unsuccessful_attempt",
                ],
            }
            row["reuse_evidence"]["route"] = {
                "requested_model": "openai/gpt-4o-mini",
                "served_model": "openai/gpt-4o-mini",
                "provider": "openai",
                "attempts": [
                    {
                        "provider": "openai",
                        "model": "openai/gpt-4o-mini",
                        "status": 504,
                    },
                    {
                        "provider": "openai",
                        "model": "openai/gpt-4o-mini",
                        "status": 200,
                    },
                ],
            }
            gateway_probe_results.validate_resume_rows(
                [row],
                experiment=experiment,
                schedule=schedule,
                schedule_digest=schedule_digest,
                price_digest=price_digest,
            )
            self.assertEqual(gateway_probe_results.RESULT_SCHEMA_VERSION, 5)
            self.assertTrue(row["cell_id"].startswith("gateway-probe-cell-v5-"))

            variants = {}
            variants["missing_route"] = copy.deepcopy(row)
            variants["missing_route"]["reuse_evidence"].pop("route")
            variants["fallback"] = copy.deepcopy(row)
            variants["fallback"]["reuse_evidence"]["route"]["attempts"][0][
                "provider"
            ] = "other"
            variants["malformed"] = copy.deepcopy(row)
            variants["malformed"]["reuse_evidence"]["route"]["attempts"][0].pop(
                "status"
            )
            variants["multiple_successes"] = copy.deepcopy(row)
            variants["multiple_successes"]["reuse_evidence"]["route"][
                "attempts"
            ][0]["status"] = 200
            variants["extra_reason"] = copy.deepcopy(row)
            variants["extra_reason"]["reuse_evidence"]["route_integrity"][
                "reasons"
            ].append("fallback_attempt")
            for name, candidate in variants.items():
                with self.subTest(name=name):
                    with self.assertRaises(GatewayProbeRunError):
                        gateway_probe_results.validate_resume_rows(
                            [candidate],
                            experiment=experiment,
                            schedule=schedule,
                            schedule_digest=schedule_digest,
                            price_digest=price_digest,
                        )

    def test_resume_rows_are_fully_bound(self):
        env = {
            gateway_run.FROZEN_PRICES_ENV: json.dumps({
                "openai/gpt-4o-mini": {
                    "input_per_million": "1",
                    "output_per_million": "2",
                    "effective_at": "2026-07-25T00:00:00Z",
                }
            }),
        }
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            spec_path.write_text(manifest(), encoding="utf-8")
            experiment = gateway_probe_spec.load_experiment(spec_path)
            schedule = gateway_probe_run.build_schedule(experiment)
            schedule_digest = gateway_spec.canonical_digest(
                [dataclasses.asdict(block) for block in schedule]
            )
            _prices, price_snapshot = gateway_run.load_frozen_prices(env)
            price_digest = gateway_spec.canonical_digest(price_snapshot)
            base = bound_row(
                experiment, schedule[0], schedule_digest, price_digest
            )
            warm_base = bound_row(
                experiment,
                next(block for block in schedule if block.condition == "warm"),
                schedule_digest,
                price_digest,
            )
            variants = {}
            variants["schema"] = copy.deepcopy(base)
            variants["schema"]["schema_version"] = 2
            variants["cell_id"] = copy.deepcopy(base)
            variants["cell_id"]["cell_id"] = "forged"
            variants["block_id"] = copy.deepcopy(base)
            variants["block_id"]["identity"]["schedule"]["block_id"] = "forged"
            variants["arm_digest"] = copy.deepcopy(base)
            variants["arm_digest"]["identity"]["arm"]["digest"] = "f" * 64
            variants["prompt_digest"] = copy.deepcopy(base)
            variants["prompt_digest"]["identity"]["case"][
                "prompt_digest"
            ] = "f" * 64
            variants["expected_arms"] = copy.deepcopy(base)
            variants["expected_arms"]["expected_arm_ids"] = [
                base["identity"]["arm"]["id"]
            ]
            variants["membership"] = copy.deepcopy(base)
            variants["membership"]["identity"]["schedule"]["repetition"] = 999
            variants["comparison"] = copy.deepcopy(base)
            variants["comparison"]["identity"]["comparison"][
                "price_digest"
            ] = "f" * 64
            variants["provenance"] = copy.deepcopy(base)
            variants["provenance"]["arm_role"] = (
                "gateway" if base["arm_role"] == "direct" else "direct"
            )
            variants["unsafe_receipt"] = copy.deepcopy(base)
            variants["unsafe_receipt"]["request_metrics"]["receipt_headers"] = {
                "authorization": "secret"
            }
            variants["unsafe_retry_receipt"] = copy.deepcopy(base)
            variants["unsafe_retry_receipt"]["retry_evidence"]["attempts"][0][
                "receipt_headers"
            ] = {"authorization": "secret"}
            variants["timing_schema"] = copy.deepcopy(base)
            variants["timing_schema"]["request_metrics"]["timing"]["ttfb_s"] = 1.0
            variants["timing_order"] = copy.deepcopy(base)
            variants["timing_order"]["request_metrics"]["timing"][
                "request_to_response_headers_s"
            ] = 0.25
            variants["timing_offset"] = copy.deepcopy(base)
            variants["timing_offset"]["request_metrics"]["timing"][
                "cold_end_to_end_semantic_ttft_s"
            ] = 0.4
            variants["success_timeout"] = copy.deepcopy(base)
            variants["success_timeout"]["outcome"].update({
                "timed_out": True,
                "error_class": "timeout",
                "error_detail": "timeout",
            })
            variants["success_without_stream"] = copy.deepcopy(base)
            variants["success_without_stream"]["request_metrics"]["stream"] = None
            variants["malformed_finish_reason"] = copy.deepcopy(base)
            variants["malformed_finish_reason"]["request_metrics"]["stream"][
                "finish_reason"
            ] = "Length"
            variants["unsafe_output_details"] = copy.deepcopy(base)
            variants["unsafe_output_details"]["request_metrics"]["usage"][
                "output_tokens_details"
            ]["private_detail"] = 1
            variants["numeric_socket_reuse"] = copy.deepcopy(base)
            variants["numeric_socket_reuse"]["reuse_evidence"][
                "socket_reused"
            ] = 1
            variants["unsafe_primer_finish_reason"] = copy.deepcopy(warm_base)
            variants["unsafe_primer_finish_reason"]["reuse_evidence"]["stream"][
                "finish_reason"
            ] = "Stop"
            variants["unbounded_primer_stream"] = copy.deepcopy(warm_base)
            variants["unbounded_primer_stream"]["reuse_evidence"]["stream"][
                "events"
            ] = 2
            variants["warm_without_reuse"] = copy.deepcopy(warm_base)
            variants["warm_without_reuse"]["reuse_evidence"][
                "socket_reused"
            ] = False
            variants["nested_private_field"] = copy.deepcopy(base)
            variants["nested_private_field"]["request_metrics"]["route"] = {
                "private_output": "must-not-persist"
            }
            variants["inconsistent_charged_cost"] = copy.deepcopy(base)
            variants["inconsistent_charged_cost"]["billing"][
                "charged_cost_usd"
            ] = "1"
            for name, candidate in variants.items():
                if name not in {
                    "schema", "cell_id", "expected_arms", "provenance"
                }:
                    candidate["cell_id"] = gateway_probe_results.cell_id(
                        candidate["identity"]
                    )
                with self.subTest(name=name):
                    with self.assertRaises(GatewayProbeRunError):
                        gateway_probe_results.validate_resume_rows(
                            [candidate],
                            experiment=experiment,
                            schedule=schedule,
                            schedule_digest=schedule_digest,
                            price_digest=price_digest,
                        )

    def test_jsonl_rejects_duplicate_cell_and_unterminated_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "results.jsonl")
            row = {
                "benchmark": gateway_probe_results.BENCHMARK,
                "cell_id": "cell",
            }
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaises(GatewayProbeRunError):
                gateway_probe_results.load_results(path)
            path.write_text(
                json.dumps(row) + "\n" + json.dumps(row) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(GatewayProbeRunError):
                gateway_probe_results.load_results(path)


if __name__ == "__main__":
    unittest.main()
