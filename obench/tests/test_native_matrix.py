from __future__ import annotations

import copy
import unittest

from obench.native_matrix import (
    NativeMatrixError,
    PILOT_REPETITIONS,
    build_native_matrix,
    reconcile_native_state,
    validate_native_matrix,
)


def _plan(*, repetitions=PILOT_REPETITIONS, arm_count=2):
    arms = [
        {
            "id": f"mcp-{letter}",
            "mcp": {
                "name": "computer-use-mcp",
                "version": f"1.0.{index}",
                "server_sha256": f"{index + 1:064x}",
            },
        }
        for index, letter in enumerate(("a", "b", "c")[:arm_count])
    ]
    return build_native_matrix(
        comparison_id="cub-v0",
        task={"name": "native-form", "content_sha256": "a" * 64},
        harness={"name": "codex", "version": "0.200.0"},
        model={"name": "gpt-fixture", "snapshot": "2026-08-06"},
        arms=arms,
        repetitions=repetitions,
    )


class NativeMatrixTests(unittest.TestCase):
    def test_default_is_deterministic_matched_ab_ba_interleaving(self):
        first = _plan()
        second = _plan()

        self.assertEqual(first, second)
        self.assertEqual(first["repetitions"], 5)
        orders = [
            [cell["arm_id"] for cell in first["schedule"] if cell["block"] == block]
            for block in range(1, 6)
        ]
        self.assertEqual(
            orders,
            [
                ["mcp-a", "mcp-b"],
                ["mcp-b", "mcp-a"],
                ["mcp-b", "mcp-a"],
                ["mcp-a", "mcp-b"],
                ["mcp-a", "mcp-b"],
            ],
        )
        for block in range(1, 6):
            self.assertEqual(
                {cell["arm_id"] for cell in first["schedule"] if cell["block"] == block},
                {"mcp-a", "mcp-b"},
            )
        self.assertFalse(first["publish_repetition_recommendation_met"])

    def test_multiple_arms_use_forward_reverse_rotations(self):
        plan = _plan(repetitions=4, arm_count=3)
        orders = [
            [cell["arm_id"] for cell in plan["schedule"] if cell["block"] == block]
            for block in range(1, 5)
        ]
        self.assertEqual(
            orders,
            [
                ["mcp-a", "mcp-b", "mcp-c"],
                ["mcp-c", "mcp-b", "mcp-a"],
                ["mcp-b", "mcp-c", "mcp-a"],
                ["mcp-a", "mcp-c", "mcp-b"],
            ],
        )

    def test_exact_plan_and_config_identities_detect_mutation(self):
        plan = _plan()
        validate_native_matrix(plan)
        changed = copy.deepcopy(plan)
        changed["fixed_identity"]["model"]["snapshot"] = "different"
        with self.assertRaisesRegex(NativeMatrixError, "canonical declared intent"):
            validate_native_matrix(changed)

    def test_resume_is_idempotent_and_never_replaces_a_cell(self):
        plan = _plan(repetitions=1)
        cell = plan["schedule"][0]
        observation = {
            key: cell[key]
            for key in ("cell_id", "trial_id", "config_sha256", "cell_sha256")
        }
        observation.update(
            {"result_sha256": "a" * 64, "bundle_sha256": "b" * 64}
        )

        state = reconcile_native_state(plan, [observation, observation])
        self.assertEqual(state["completed"], [observation])
        resumed = reconcile_native_state(
            plan, [observation], prior_state=state
        )
        self.assertEqual(resumed["completed"], [observation])
        conflicting = {**observation, "result_sha256": "c" * 64}
        with self.assertRaisesRegex(NativeMatrixError, "different immutable"):
            reconcile_native_state(plan, [conflicting], prior_state=state)

    def test_resume_rejects_reassigned_trial_or_config(self):
        plan = _plan(repetitions=1)
        cell = plan["schedule"][0]
        base = {
            key: cell[key]
            for key in ("cell_id", "trial_id", "config_sha256", "cell_sha256")
        }
        base.update({"result_sha256": "a" * 64, "bundle_sha256": "b" * 64})
        for field in ("trial_id", "config_sha256", "cell_sha256"):
            bad = {**base, field: "c" * 64}
            with self.assertRaisesRegex(NativeMatrixError, f"conflicting {field}"):
                reconcile_native_state(plan, [bad])


if __name__ == "__main__":
    unittest.main()
