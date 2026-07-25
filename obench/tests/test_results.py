import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from obench import results


def digest(label):
    return results.canonical_digest({"identity": label})


def gateway_identity():
    return results.CellIdentity.for_gateway(
        track="fixed_model_provider",
        experiment_id="gateway-bench-smoke",
        experiment_digest=digest("experiment"),
        arm_id="via-openrouter",
        arm_digest=digest("arm"),
        policy_digest=digest("policy"),
        catalog_digest=digest("catalog"),
        price_digest=digest("price"),
        sampling_digest=digest("sampling"),
        schedule_digest=digest("schedule"),
        provider_prompt_mode="provider_default",
        task="make-it-run",
        task_digest=digest("task"),
        checker_digest=digest("checker"),
        workspace_source_sha="a" * 40,
        harness="pi",
        candidate="openrouter-candidate",
        harness_version="0.80.10",
        execution_lane="docker",
        image_digest=digest("image"),
        budget_timeout_s=300,
        budget_max_calls=8,
        budget_max_output_tokens=16000,
        budget_usd_cap="2.50",
        adapter_timeout_s=240,
        checker_timeout_s=60,
        window_id="morning",
        repetition=1,
        block_id="block-001",
        block_attempt=0,
    )


SHARED_CHANGES = {
    "track": "other_gateway_track",
    "experiment_id": "other-experiment",
    "experiment_digest": digest("experiment-changed"),
    "policy_digest": digest("policy-changed"),
    "catalog_digest": digest("catalog-changed"),
    "price_digest": digest("price-changed"),
    "sampling_digest": digest("sampling-changed"),
    "schedule_digest": digest("schedule-changed"),
    "provider_prompt_mode": "isolated_per_call_v1",
    "harness": "other-harness",
    "candidate": "other-candidate",
    "harness_version": "0.80.11",
    "execution_lane": "local",
    "image_digest": digest("image-changed"),
    "budget_timeout_s": 301,
    "budget_max_calls": 9,
    "budget_max_output_tokens": 16001,
    "budget_usd_cap": "2.51",
    "adapter_timeout_s": 241,
    "checker_timeout_s": 61,
}

CELL_ONLY_CHANGES = {
    "arm_id": "direct-openai",
    "arm_digest": digest("arm-changed"),
    "task": "other-task",
    "task_digest": digest("task-changed"),
    "checker_digest": digest("checker-changed"),
    "workspace_source_sha": "b" * 40,
    "window_id": "evening",
    "repetition": 2,
    "block_id": "block-002",
    "block_attempt": 1,
}


class CanonicalJsonTests(unittest.TestCase):
    def test_canonical_json_is_stable_compact_and_utf8(self):
        left = {"z": [3, 2], "a": {"unicode": "caf\u00e9"}}
        right = {"a": {"unicode": "caf\u00e9"}, "z": [3, 2]}

        self.assertEqual(results.canonical_json(left), results.canonical_json(right))
        self.assertEqual(results.canonical_digest(left), results.canonical_digest(right))
        self.assertEqual(
            results.canonical_json(left),
            '{"a":{"unicode":"caf\u00e9"},"z":[3,2]}',
        )

    def test_canonical_json_rejects_non_json_floats(self):
        with self.assertRaises(ValueError):
            results.canonical_json({"value": float("nan")})


class GatewayIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = gateway_identity()

    def test_schema_v2_identity_is_immutable_and_canonically_nested(self):
        self.assertEqual(self.identity.schema_version, 2)
        self.assertEqual(
            set(self.identity.as_dict()),
            {
                "schema_version", "benchmark", "experiment", "arm", "comparison",
                "task", "harness", "execution", "schedule",
            },
        )
        self.assertEqual(
            self.identity.as_dict()["execution"]["budget"],
            {
                "timeout_s": 300,
                "max_calls": 8,
                "max_output_tokens": 16000,
                "usd_cap": "2.50",
            },
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.identity.task = "other"

    def test_every_normative_dimension_changes_cell_id(self):
        original = results.make_gateway_cell_id(self.identity)
        for field, replacement in {**SHARED_CHANGES, **CELL_ONLY_CHANGES}.items():
            with self.subTest(field=field):
                changed = dataclasses.replace(self.identity, **{field: replacement})
                self.assertNotEqual(original, results.make_gateway_cell_id(changed))

    def test_comparison_shared_dimensions_change_run_id(self):
        original = results.make_gateway_run_id(self.identity)
        for field, replacement in SHARED_CHANGES.items():
            with self.subTest(field=field):
                changed = dataclasses.replace(self.identity, **{field: replacement})
                self.assertNotEqual(original, results.make_gateway_run_id(changed))

    def test_cell_specific_dimensions_do_not_split_run_id(self):
        original = results.make_gateway_run_id(self.identity)
        for field, replacement in CELL_ONLY_CHANGES.items():
            with self.subTest(field=field):
                changed = dataclasses.replace(self.identity, **{field: replacement})
                self.assertEqual(original, results.make_gateway_run_id(changed))

    def test_fixed_benchmark_and_schema_are_normative(self):
        for changes in (
            {"schema_version": 1},
            {"benchmark": "harness"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(results.ResultIdentityError):
                    dataclasses.replace(self.identity, **changes)

    def test_nested_validation_round_trips_and_rejects_shape_drift(self):
        nested = self.identity.as_dict()
        self.assertEqual(results.validate_gateway_identity(nested), self.identity)

        missing = dict(nested)
        missing.pop("comparison")
        extra = dict(nested)
        extra["label"] = "not normative"
        bad_nested = dict(nested)
        bad_nested["task"] = dict(nested["task"], extra="not normative")
        for candidate in (missing, extra, bad_nested):
            with self.subTest(candidate=candidate):
                with self.assertRaises(results.ResultIdentityError):
                    results.validate_gateway_identity(candidate)


class DispatchTests(unittest.TestCase):
    def test_metadata_free_rows_dispatch_as_legacy_harness_results(self):
        row = {"run_id": "pi:task:model:trial1"}

        self.assertEqual(results.result_kind(row), (0, "harness"))
        self.assertEqual(
            results.dispatch_result(
                row,
                {(0, "harness"): lambda value: value["run_id"]},
            ),
            row["run_id"],
        )

    def test_gateway_results_dispatch_as_schema_v2(self):
        row = {"schema_version": 2, "benchmark": "gateway"}

        self.assertEqual(results.result_kind(row), (2, "gateway"))
        with self.assertRaises(results.UnsupportedResultError):
            results.dispatch_result(row, {(1, "gateway"): lambda value: value})

    def test_invalid_schema_version_fails_closed(self):
        for version in (True, -1, "2"):
            with self.subTest(version=version):
                with self.assertRaises(results.ResultError):
                    results.result_kind({
                        "schema_version": version,
                        "benchmark": "gateway",
                    })


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "results.jsonl"
        self.identity = gateway_identity()

    def tearDown(self):
        self.temp_dir.cleanup()

    def gateway_row(self, identity=None):
        identity = identity or self.identity
        return {
            "schema_version": 2,
            "benchmark": "gateway",
            "identity": identity.as_dict(),
            "provider_prompt_mode": identity.provider_prompt_mode,
            "run_id": results.make_gateway_run_id(identity),
            "cell_id": results.make_gateway_cell_id(identity),
        }

    def write_rows(self, rows):
        self.path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_missing_file_is_empty_resume_state(self):
        self.assertEqual(
            results.read_jsonl_for_resume(self.path),
            results.ResumeState((), frozenset()),
        )

    def test_reads_legacy_and_nested_v2_rows(self):
        legacy = {"run_id": "pi:task:model:trial1", "success": True}
        gateway = self.gateway_row()
        self.write_rows([legacy, gateway])

        state = results.read_jsonl_for_resume(self.path)

        self.assertEqual(state.rows, (legacy, gateway))
        self.assertEqual(
            state.cell_ids,
            frozenset({legacy["run_id"], gateway["cell_id"]}),
        )

    def test_gateway_row_requires_canonical_nested_identity(self):
        row = self.gateway_row()
        row.pop("identity")
        self.write_rows([row])

        with self.assertRaisesRegex(results.ResultsLogError, "identity is required"):
            results.read_jsonl_for_resume(self.path)

    def test_same_run_id_is_allowed_for_distinct_cells(self):
        other = dataclasses.replace(
            self.identity,
            arm_id="direct-openai",
            arm_digest=digest("direct-arm"),
            task="other-task",
            task_digest=digest("other-task"),
            checker_digest=digest("other-checker"),
            workspace_source_sha="b" * 40,
            window_id="evening",
            repetition=2,
            block_id="block-002",
        )
        rows = [self.gateway_row(), self.gateway_row(other)]
        self.assertEqual(rows[0]["run_id"], rows[1]["run_id"])
        self.write_rows(rows)

        self.assertEqual(len(results.read_jsonl_for_resume(self.path).cell_ids), 2)

    def test_duplicate_legacy_or_gateway_cells_are_rejected(self):
        cases = (
            [{"run_id": "same"}, {"run_id": "same"}],
            [self.gateway_row(), self.gateway_row()],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                self.write_rows(rows)
                with self.assertRaises(results.DuplicateResultError):
                    results.read_jsonl_for_resume(self.path)

    def test_corrupt_truncated_and_ambiguous_lines_fail_closed(self):
        cases = (
            b'{"run_id":"ok"}\n{"run_id":',
            b'{"run_id":"valid-json-but-no-append-boundary"}',
            b'{"run_id":"ok"}\n[]\n',
            b'{"run_id":"ok"}\n\xff\n',
            b'{"run_id":"first","run_id":"second"}\n',
            b'{"run_id":"ok","value":NaN}\n',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(results.ResultsLogError):
                    results.read_jsonl_for_resume(self.path)

    def test_invalid_path_and_legacy_identity_fail_closed(self):
        self.path.mkdir()
        with self.assertRaisesRegex(results.ResultsLogError, "not a regular file"):
            results.read_jsonl_for_resume(self.path)

        self.path.rmdir()
        self.write_rows([{"success": True}])
        with self.assertRaisesRegex(results.ResultsLogError, "legacy row run_id"):
            results.read_jsonl_for_resume(self.path)

    def test_gateway_ids_and_dispatch_metadata_must_match_identity(self):
        cases = (
            ("cell_id", "gateway-cell-v2-wrong", "cell_id does not match"),
            ("run_id", "gateway-run-v2-wrong", "run_id does not match"),
            ("schema_version", 1, "schema_version conflicts"),
            ("benchmark", "harness", "benchmark conflicts"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                row = self.gateway_row()
                row[field] = value
                self.write_rows([row])
                with self.assertRaisesRegex(results.ResultsLogError, message):
                    results.read_jsonl_for_resume(self.path)


if __name__ == "__main__":
    unittest.main()
