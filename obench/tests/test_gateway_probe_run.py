import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench import (
    gateway_probe_http,
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


def canned_result(*, stop_required=False):
    return {
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
            "connection": {
                "dns_s": 0.01,
                "tcp_s": 0.01,
                "tls_s": 0.01,
            },
            "timing": {
                "ttfb_s": 0.1,
                "semantic_ttft_s": 0.2,
                "total_s": 0.3,
            },
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
            "stream": {"done": True, "terminal_status": "completed"},
            "coverage": {},
        },
        "reuse_evidence": {
            "required": False,
            "completed": False,
            "http_status": None,
            "socket_reused": None,
            "primer_nonce_sha256": "1" * 64,
            "measured_nonce_sha256": "2" * 64,
            "connection": {},
            "route_integrity": None,
            "usage": None,
            "cache": None,
            "costs": {},
        },
        "billing": {
            "primer_cost_usd": None,
            "measured_cost_usd": "1.00" if stop_required else "0",
            "charged_cost_usd": "1.00" if stop_required else "0",
            "stop_required": stop_required,
        },
    }


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
                    )

    def test_cumulative_cap_stops_after_paid_partial_block_and_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                return_value=canned_result(stop_required=True),
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

    def test_run_resumes_complete_blocks_and_replaces_partial_latest_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp, "probe.toml")
            results_path = Path(tmp, "probe.jsonl")
            spec_path.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_http,
                "execute_request",
                return_value=canned_result(),
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
