import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench.atif import validate_trajectory


ADAPTER_PATH = Path(__file__).parents[1] / "adapters" / "codex.py"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "codex_native_mcp.jsonl"


def load_codex():
    spec = importlib.util.spec_from_file_location("test_codex_native_adapter", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def fixture_stdout():
    return "\n".join(
        line
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ) + "\n"


class CodexNativeProfileTests(unittest.TestCase):
    def setUp(self):
        self.codex = load_codex()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.attempt = self.root / "attempt 1"
        self.attempt.mkdir()
        self.launcher = self.attempt / "collector 'quoted'; echo unsafe"
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.auth_home = self.root / "source-auth"
        self.auth_home.mkdir()
        (self.auth_home / "auth.json").write_text('{"tokens":"fixture"}', encoding="utf-8")
        (self.auth_home / "config.toml").write_text("untrusted = true\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def native_env(self):
        return {
            "CODEX_HOME": str(self.auth_home),
            "CUB_MCP_COMMAND": str(self.launcher),
            "OPENBENCH_NATIVE_TRIAL_ID": "trial-1",
            "OPENBENCH_NATIVE_MCP_LEDGER": str(self.attempt / "mcp-ledger.jsonl"),
        }

    def test_stock_command_and_artifacts_are_unchanged(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout=fixture_stdout())

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.auth_home)}, clear=True), \
                mock.patch.object(self.codex.subprocess, "run", side_effect=fake_run):
            result = self.codex.run("do it", str(self.workspace), "gpt-5.6-sol", 10)

        self.assertTrue(result["completed"])
        cmd, _kwargs = calls[0]
        self.assertFalse(any("mcp_servers.computer-use" in arg for arg in cmd))
        self.assertFalse((self.workspace / "trajectory.json").exists())
        self.assertFalse((self.attempt / "codex-events.jsonl").exists())

    def test_native_profile_injects_exact_shell_safe_mcp_and_writes_atif(self):
        calls = []
        stdout = fixture_stdout()

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs, tuple(sorted(Path(kwargs["env"]["CODEX_HOME"]).iterdir()))))
            return FakeProc(stdout=stdout)

        supplied_home = self.root / "caller-home-with-config"
        supplied_home.mkdir()
        (supplied_home / "config.toml").write_text("mcp_servers.bad.command='bad'\n", encoding="utf-8")
        env_override = self.native_env() | {"CODEX_HOME": str(supplied_home)}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.auth_home)}, clear=True), \
                mock.patch.object(self.codex.subprocess, "run", side_effect=fake_run):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10,
                env_override=env_override,
            )

        self.assertTrue(result["completed"], result)
        self.assertEqual(result["tokens"], 24)
        self.assertEqual(result["turns"], 2)
        cmd, kwargs, isolated_files = calls[0]
        self.assertEqual(cmd[cmd.index("-C") + 1], str(self.workspace))
        overrides = [cmd[index + 1] for index, arg in enumerate(cmd[:-1]) if arg == "-c"]
        self.assertIn(
            f"mcp_servers.computer-use.command={json.dumps(str(self.launcher))}",
            overrides,
        )
        self.assertIn("mcp_servers.computer-use.args=[]", overrides)
        self.assertIn('model_reasoning_effort="medium"', overrides)
        self.assertIn('service_tier="default"', overrides)
        self.assertEqual(cmd[-1], "use the app")
        self.assertNotEqual(kwargs["env"]["CODEX_HOME"], str(supplied_home))
        self.assertEqual([path.name for path in isolated_files], ["auth.json"])

        raw_path = self.attempt / "codex-events.jsonl"
        self.assertEqual(raw_path.read_text(encoding="utf-8"), stdout)
        self.assertEqual(stat.S_IMODE(raw_path.stat().st_mode), 0o600)
        trajectory_path = self.workspace / "trajectory.json"
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertEqual(trajectory["agent"]["model_name"], "gpt-5.6-sol")
        self.assertEqual(trajectory["extra"]["turn_count"], 2)
        self.assertEqual(trajectory["extra"]["source_transcript"], "codex-events.jsonl")
        self.assertNotIn("clicked secret target", trajectory_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(trajectory_path.stat().st_mode), 0o600)

    def test_native_profile_requires_fixed_model(self):
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(self.codex.subprocess, "run") as run:
            result = self.codex.run("do it", str(self.workspace), "gpt-5.5-medium", 10)
        self.assertFalse(result["completed"])
        self.assertTrue(result["startup_failure"])
        self.assertIn("model must be 'gpt-5.6-sol'", result["error"])
        run.assert_not_called()

    def test_native_profile_keeps_raw_jsonl_and_fails_closed_on_invalid_source(self):
        stdout = '{"type":"thread.started","thread_id":"thread-1"}\nnot-json\n'
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    return_value=FakeProc(stdout=stdout),
                ):
            result = self.codex.run("do it", str(self.workspace), "gpt-5.6-sol", 10)

        self.assertFalse(result["completed"])
        self.assertIn("native ATIF conversion failed", result["error"])
        self.assertEqual(
            (self.attempt / "codex-events.jsonl").read_text(encoding="utf-8"),
            stdout,
        )
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_profile_rejects_missing_or_invalid_launcher(self):
        invalid = self.root / "not-executable"
        invalid.write_text("no", encoding="utf-8")
        cases = (
            {"OPENBENCH_NATIVE_TRIAL_ID": "trial-1"},
            self.native_env() | {"CUB_MCP_COMMAND": "relative-launcher"},
            self.native_env() | {"CUB_MCP_COMMAND": str(self.root / "missing")},
            self.native_env() | {"CUB_MCP_COMMAND": str(invalid)},
        )
        for env in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(self.codex.subprocess, "run") as run:
                result = self.codex.run("do it", str(self.workspace), "gpt-5.6-sol", 10)
            self.assertFalse(result["completed"])
            self.assertTrue(result["startup_failure"])
            self.assertIn("absolute executable file", result["error"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
