"""Synthetic session evidence only: no copied user prompts or real rollouts."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = json.loads((HERE / "tests.json").read_text())
package = Path.cwd() / "scripts/profiles"
spec = importlib.util.spec_from_file_location(
    "dojo_profiles", package / "__init__.py", submodule_search_locations=[str(package)])
module = importlib.util.module_from_spec(spec)
sys.modules["dojo_profiles"] = module
spec.loader.exec_module(module)
from dojo_profiles import budget, rollout_codex
from dojo_profiles.probe_codex import parse_block

DOJO = "/synthetic/.agents/skills/review/SKILL.md"
BUNDLED = "/synthetic/.codex/skills/.system/review/SKILL.md"
CONNECTOR = "/synthetic/.codex/plugins/cache/openai-curated-remote/demo/1/review/SKILL.md"


def block(entries, roots=None):
    # These are input schema literals, not values obtained from the implementation.
    lines = ["<skills_instructions>", "## Skills"]
    if roots:
        lines += ["a short path that can be expanded into an absolute path using the skill roots table",
                  "### Skill roots", *roots]
    else:
        lines += ["Each entry includes a name, description, and source locator."]
    lines += ["### Available skills"]
    lines += [f"- {name}: Synthetic description. (file: {locator})" for name, locator in entries]
    return "\n".join([*lines, "</skills_instructions>"])


def record(role, text):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}}


def read(records):
    meta = {"type": "session_meta", "payload": {"originator": "codex-tui",
            "cli_version": "fixture", "cwd": "/synthetic/work", "model": "fixture"}}
    with tempfile.TemporaryDirectory(prefix="dojo-evidence-fixture-") as tmp:
        path = Path(tmp) / "rollout-synthetic.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in [meta, *records]) + "\n")
        return rollout_codex.read_rollout(path)


def observation(entries, roots=None):
    return read([record("developer", block(entries, roots))])


class RolloutTests(unittest.TestCase):
    def test_developer_listing_is_accepted(self):
        found = observation([("live", DOJO)])
        self.assertIsNotNone(found)
        self.assertEqual([entry.name for entry in found.listing.entries], ["live"])
        self.assertEqual(found.meta.surface, "codex-tui")

    def test_decoys_cannot_replace_developer_listing(self):
        decoy = block([("quoted", CONNECTOR)])
        rows = [record(role, decoy) for role in ("user", "assistant", "tool")]
        rows += [{"type": "compacted", "payload": {"message": decoy}}]
        for row in rows:
            with self.subTest(row=row["type"], role=row["payload"].get("role")):
                found = read([row, record("developer", block([("actual", DOJO)]))])
                self.assertEqual([entry.name for entry in found.listing.entries], ["actual"])

    def test_decoys_alone_are_not_observations(self):
        decoy = block([("quoted", CONNECTOR)])
        rows = [record(role, decoy) for role in ("user", "assistant", "tool")]
        rows += [{"type": "compacted", "payload": {"message": decoy}}]
        self.assertIsNone(read(rows))

    def test_empty_developer_listing_remains_an_observation(self):
        found = observation([])
        self.assertIsNotNone(found)
        self.assertEqual(found.listing.entries, [])
        self.assertIsNone(read([record("developer", "No skills listing in this context.")]))


def policy():
    return budget.Policy(
        harness="codex", harness_version="fixture", model="fixture", unit="tokens",
        limit=4000, context_window=None, window_field=None, estimator="fixture",
        provenance="synthetic", measured="fixture", probe="fixture", deployable=True,
        shadows_by_name=False, project_scope_root=".agents/skills", limit_basis="observed",
        declared_surfaces=("codex-tui",))


ENTRIES = [{"name": "review", "source_description": "Short description.",
            "listed_description": "Short description.", "locator": DOJO}]


class BudgetTests(unittest.TestCase):
    def test_declared_surface_can_deploy_and_gate(self):
        result = budget.assess(ENTRIES, policy(), surface="codex-tui")
        self.assertEqual(result.verdict, budget.Verdict.DEPLOYABLE)
        self.assertTrue(result.gating)
        self.assertGreater(result.demand, 0)
        self.assertEqual(result.entries_scored, 1)

    def test_wrong_surface_is_unsupported_and_not_gating(self):
        for surface in ("exec", "codex_exec"):
            with self.subTest(surface=surface):
                result = budget.assess(ENTRIES, policy(), surface=surface)
                self.assertEqual(result.verdict, budget.Verdict.UNSUPPORTED)
                self.assertFalse(result.gating)
                self.assertEqual(result.entries_scored, 0)

    def test_missing_surface_is_unsupported_and_not_gating(self):
        result = budget.assess(ENTRIES, policy())
        self.assertEqual(result.verdict, budget.Verdict.UNSUPPORTED)
        self.assertFalse(result.gating)
        self.assertEqual(result.entries_scored, 0)

    def test_direct_assessments_enforce_surface_when_gating(self):
        for surface in (None, "exec", "codex-tui"):
            with self.subTest(surface=surface):
                result = budget.Assessment(policy(), 3999, 4000, "tokens",
                                           budget.Verdict.NONCONFORMANT, surface=surface)
                self.assertEqual(result.gating, surface == "codex-tui")


class MismatchTests(unittest.TestCase):
    def assert_differences(self, actual, count):
        # Return-shape and multiplicity are contractual. Display encoding is
        # not: bare names, qualified names, and other readable labels work.
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), count)
        self.assertTrue(all(isinstance(label, str) and label for label in actual))

    def test_equivalent_qualified_listings_compare_equal(self):
        live = parse_block(block([("review", DOJO), ("review", CONNECTOR)]))
        recorded = observation([("review", "r0/demo/2/review/SKILL.md"), ("review", DOJO)],
            ["- `r0` = `/synthetic/.codex/plugins/cache/openai-curated-remote`"])
        self.assertEqual(len(recorded.listing.entries), 2)
        self.assertIsNone(rollout_codex.surface_mismatch(live, recorded))

    def test_losing_same_name_from_another_origin_is_reported(self):
        live = parse_block(block([("review", DOJO)]))
        recorded = observation([("review", DOJO), ("review", BUNDLED)])
        result = rollout_codex.surface_mismatch(live, recorded)
        self.assertIsNotNone(result)
        self.assertEqual((result["live_entries"], result["recorded_entries"]), (1, 2))
        self.assert_differences(result["only_in_recorded"], 1)
        self.assertEqual(result["only_in_live"], [])

    def test_duplicate_multiplicity_is_reported(self):
        live = parse_block(block([("review", DOJO)]))
        recorded = observation([("review", DOJO)] * 3)
        result = rollout_codex.surface_mismatch(live, recorded)
        self.assertIsNotNone(result)
        self.assertEqual((result["live_entries"], result["recorded_entries"]), (1, 3))
        self.assert_differences(result["only_in_recorded"], 2)
        self.assertEqual(result["only_in_live"], [])

    def test_origin_swap_with_same_name_and_count_is_reported(self):
        live = parse_block(block([("review", DOJO)]))
        result = rollout_codex.surface_mismatch(live, observation([("review", CONNECTOR)]))
        self.assertIsNotNone(result)
        self.assertEqual((result["live_entries"], result["recorded_entries"]), (1, 1))
        self.assert_differences(result["only_in_recorded"], 1)
        self.assert_differences(result["only_in_live"], 1)


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []

    def startTest(self, test):
        self.executed.append(test.id().split(".", 1)[1])
        super().startTest(test)


if __name__ == "__main__":
    names = SPEC[sys.argv[1]]
    suite = unittest.TestLoader().loadTestsFromNames(names, sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    report = {"executed": result.executed, "count": result.testsRun,
              "success": result.wasSuccessful(), "skipped": len(result.skipped)}
    print("ORACLE_RESULT:" + json.dumps(report))
    raise SystemExit(0 if result.wasSuccessful() else 1)
