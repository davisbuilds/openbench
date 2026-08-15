import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

from obench.validate_tasks import discover_tasks, run_checker


ROOT = Path(__file__).resolve().parents[2] / "computer-use-tasks" / "v0"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ComputerUseTasksTests(unittest.TestCase):
    def run_verifier(self, task_name, workspace, extra_env=None):
        task = ROOT / task_name
        env = dict(**__import__("os").environ)
        env["TASK_DIR"] = str(task)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(task / "checker.sh")],
            cwd=workspace,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )

    def solved_workspace(self, task_name):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        shutil.copytree(ROOT / task_name / "workspace", root, dirs_exist_ok=True)
        shutil.copytree(ROOT / task_name / "solution", root, dirs_exist_ok=True)
        return temporary, root

    def test_common_envelopes_and_native_sidecars_are_explicit(self):
        expected_revisions = {
            "basic-controls": "d2b345eeb96ad5d27f8200f4e6c40cba5d2010de",
            "background-control": "3516eca731d86a1e2f1a3fe203709ecb8940c3b3",
            "post-action-state-ab": "d2b345eeb96ad5d27f8200f4e6c40cba5d2010de",
        }
        for name in (
            "basic-controls",
            "background-control",
            "post-action-state-ab",
            "state-response-ab",
            "system-settings-discovery",
            "textedit-exact-file",
        ):
            with self.subTest(task=name):
                task = tomllib.loads((ROOT / name / "task.toml").read_text(encoding="utf-8"))
                native = tomllib.loads(
                    (ROOT / name / "native-macos.toml").read_text(encoding="utf-8")
                )
                self.assertEqual(task["schema_version"], "1.4")
                self.assertFalse(task["metadata"]["harbor_execution_supported"])
                self.assertEqual(native["schema_version"], "openbench.native-task.v1")
                self.assertEqual(native["platform"], "macos")
                self.assertFalse(native["harbor_execution_supported"])
                if name in expected_revisions:
                    self.assertEqual(native["fixture"]["revision"], expected_revisions[name])

    def test_all_primary_tasks_have_offline_polarity(self):
        tasks = discover_tasks([("computer-use-v0", str(ROOT))])
        self.assertEqual([item[1] for item in tasks], [
            "background-control",
            "basic-controls",
            "post-action-state-ab",
            "state-response-ab",
            "system-settings-discovery",
            "textedit-exact-file",
        ])
        for _tier, name, task_dir in tasks:
            with self.subTest(task=name):
                bare, bare_output, _ = run_checker(task_dir, False)
                solved, solved_output, _ = run_checker(task_dir, True)
                self.assertNotEqual(bare, 0, bare_output)
                self.assertEqual(solved, 0, solved_output)

    def test_system_settings_result_is_hash_bound_observed_and_sanitized(self):
        task_name = "system-settings-discovery"
        apple_name = "OpenBench Test User"
        expected_name_hash = hashlib.sha256(apple_name.encode("utf-8")).hexdigest()

        temporary, root = self.solved_workspace(task_name)
        self.addCleanup(temporary.cleanup)
        result = self.run_verifier(task_name, root)
        self.assertEqual(result.returncode, 0, result.stdout)
        artifact_path = root / "artifacts/discovery-result.json"
        artifact_text = artifact_path.read_text(encoding="utf-8")
        artifact = json.loads(artifact_text)
        self.assertEqual(set(artifact), {
            "apple_account_name_sha256",
            "schema_version",
            "wallpaper",
            "wallpaper_sha256",
        })
        self.assertEqual(artifact["apple_account_name_sha256"], expected_name_hash)
        self.assertNotIn(apple_name, artifact_text)

        for mutation, expected_error in (
            ("raw-evidence", "completed get_app_state"),
            ("oracle-hash", "local oracle"),
            ("final-schema", "exact two-field JSON object"),
        ):
            with self.subTest(mutation=mutation):
                case, case_root = self.solved_workspace(task_name)
                try:
                    trajectory_path = case_root / "trajectory.json"
                    events_path = case_root / "codex-events.jsonl"
                    if mutation == "raw-evidence":
                        events_path.write_text(
                            events_path.read_text(encoding="utf-8").replace(
                                '"tool":"get_app_state"', '"tool":"click"'
                            ),
                            encoding="utf-8",
                        )
                    elif mutation == "oracle-hash":
                        for path in (trajectory_path, events_path):
                            path.write_text(
                                path.read_text(encoding="utf-8").replace(
                                    apple_name, "Changed Test User"
                                ),
                                encoding="utf-8",
                            )
                    else:
                        trajectory = json.loads(
                            trajectory_path.read_text(encoding="utf-8")
                        )
                        answer = json.loads(trajectory["steps"][-1]["message"])
                        answer["email"] = "must-not-be-accepted@example.invalid"
                        trajectory["steps"][-1]["message"] = json.dumps(answer)
                        trajectory_path.write_text(
                            json.dumps(trajectory), encoding="utf-8"
                        )
                    rejected = self.run_verifier(task_name, case_root)
                    self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
                    self.assertIn(expected_error, rejected.stdout)
                finally:
                    case.cleanup()

    def test_system_settings_rejects_protected_state_mutation(self):
        temporary, root = self.solved_workspace("system-settings-discovery")
        self.addCleanup(temporary.cleanup)
        protected = root / "protected-settings.plist"
        unchanged = root / "unchanged-settings.plist"
        protected.write_bytes(b"before")
        unchanged.write_bytes(b"unchanged")
        before = root / "system-settings-before.json"
        before.write_text(
            json.dumps({
                "schema_version": "openbench.system-settings-before.v1",
                "files": [
                    {
                        "path": str(protected),
                        "sha256": hashlib.sha256(b"before").hexdigest(),
                    },
                    {
                        "path": str(unchanged),
                        "sha256": hashlib.sha256(b"unchanged").hexdigest(),
                    },
                ],
            }),
            encoding="utf-8",
        )
        protected.write_bytes(b"after")

        result = self.run_verifier(
            "system-settings-discovery",
            root,
            {"OPENBENCH_SYSTEM_SETTINGS_BEFORE_PATH": str(before)},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("changed", result.stdout.lower())

    def test_post_action_state_ab_reuses_basic_controls_checker(self):
        self.assertEqual(
            (ROOT / "post-action-state-ab/checker_data/verify.py").read_bytes(),
            (ROOT / "basic-controls/checker_data/verify.py").read_bytes(),
        )

    def test_post_action_state_initial_call_disables_screenshot_explicitly(self):
        instruction = (ROOT / "post-action-state-ab/instruction.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '1. Call `get_app_state` once with '
            '`app="org.openbench.ComputerUseFixture.v0"`\n'
            '   and `include_screenshot=false`.',
            instruction,
        )

    def test_basic_controls_rejects_extra_schema_fields(self):
        temporary, root = self.solved_workspace("basic-controls")
        self.addCleanup(temporary.cleanup)
        path = root / "artifacts/basic-controls-state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["tool_claimed_success"] = True
        path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_verifier("basic-controls", root)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_basic_controls_instruction_is_context_efficient(self):
        instruction = (ROOT / "basic-controls/instruction.md").read_text(
            encoding="utf-8"
        )
        for requirement in (
            "Inspect state once",
            "reuse the returned element IDs",
            "`include_state=false`",
            "`include_screenshot=false`",
            "do not re-read the full tree",
            "external deterministic checker",
            "Turn `toggle-box` on.",
            "Press `honest-button` exactly twice so the counter is `2`.",
            "Enter the exact text `openbench-42` in `keystroke-input`.",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, instruction)

    def test_background_rejects_target_activation_even_with_valid_seal(self):
        temporary, root = self.solved_workspace("background-control")
        self.addCleanup(temporary.cleanup)
        seal_path = root / "artifacts/focus-seal.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        seal["target_activation_observed"] = True
        seal_path.write_text(json.dumps(seal), encoding="utf-8")
        result = self.run_verifier("background-control", root)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("target activation", result.stdout)

    def test_background_instruction_uses_attached_menu_path(self):
        instruction = (ROOT / "background-control/instruction.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("`Open Menu > Fixture Menu Item`", instruction)
        self.assertIn("`mouse_button` set to `right`", instruction)
        self.assertIn("do not use\n   `click_menu_item`", instruction)
        self.assertIn("call `click`\n   once", instruction)
        self.assertNotIn("twice on the returned", instruction)

    def test_background_rejects_global_delivery_with_resealed_chain(self):
        temporary, root = self.solved_workspace("background-control")
        self.addCleanup(temporary.cleanup)
        ledger_path = root / "artifacts/focus-ledger.jsonl"
        records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        records[1]["payload"]["delivery_tier"] = "global-cg-event"
        previous = "0" * 64
        lines = []
        for record in records:
            record["previous_hash"] = previous
            record.pop("record_hash", None)
            record["record_hash"] = hashlib.sha256(canonical(record)).hexdigest()
            previous = record["record_hash"]
            lines.append(canonical(record).decode("utf-8"))
        ledger_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        ledger_path.write_bytes(ledger_bytes)
        seal_path = root / "artifacts/focus-seal.json"
        seal = json.loads(seal_path.read_text())
        seal["root_hash"] = previous
        seal["ledger_sha256"] = hashlib.sha256(ledger_bytes).hexdigest()
        seal["global_delivery_observed"] = True
        seal_path.write_text(json.dumps(seal), encoding="utf-8")
        result = self.run_verifier("background-control", root)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("global or unsupported delivery", result.stdout)

    def test_textedit_requires_exact_bytes_path_and_no_extras(self):
        mutations = ("bytes", "path", "extra")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                temporary, root = self.solved_workspace("textedit-exact-file")
                try:
                    output = root / "artifacts/openbench-exact.txt"
                    env = None
                    if mutation == "bytes":
                        output.write_bytes(output.read_bytes().rstrip(b"\n"))
                    elif mutation == "path":
                        env = {"OPENBENCH_NATIVE_OUTPUT_PATH": str(root / "artifacts/wrong.txt")}
                    else:
                        (root / "artifacts/extra.txt").write_text("extra", encoding="utf-8")
                    result = self.run_verifier("textedit-exact-file", root, env)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                finally:
                    temporary.cleanup()

    def test_controlled_diagnostics_have_exact_classifications(self):
        expected = {
            "stale-element.json": ("verifier_ambiguous", "targeting"),
            "no-effect-liar.json": ("effect_not_verified", "verification"),
            "unsupported.json": ("unsupported", "unsupported"),
            "user-interference.json": ("user_interference", "focus_safety"),
        }
        directory = ROOT / "diagnostics/v1"
        self.assertEqual(
            {path.name for path in directory.glob("*.json")}, set(expected)
        )
        fields = {
            "classification", "failure_domain", "fixture", "observed_effect",
            "schema_version", "stimulus",
        }
        for name, pair in expected.items():
            with self.subTest(fixture=name):
                value = json.loads((directory / name).read_text(encoding="utf-8"))
                self.assertEqual(set(value), fields)
                self.assertEqual(value["schema_version"], "openbench.computer-use.diagnostic.v1")
                self.assertIs(value["observed_effect"], False)
                self.assertEqual((value["classification"], value["failure_domain"]), pair)


if __name__ == "__main__":
    unittest.main()
