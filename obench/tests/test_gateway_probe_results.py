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
            "connection": {},
            "timing": {},
            "usage": None,
            "generation": None,
            "cache": {},
            "route": None,
            "costs": {},
            "stream": None,
            "coverage": None,
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
            "measured_cost_usd": "0",
            "charged_cost_usd": "0",
            "stop_required": True,
        },
    }


class GatewayProbeResultsTests(unittest.TestCase):
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
            variants = {}
            variants["schema"] = copy.deepcopy(base)
            variants["schema"]["schema_version"] = 999
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
