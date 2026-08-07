import hashlib
import importlib.util
import json
import os
import stat
import subprocess
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


def write_fixture_policy_ledger(path):
    encoded_input = json.dumps(
        {"x": 420, "y": 180},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_text(
        json.dumps({
            "tool_name": "mcp__computer-use__click",
            "tool_use_id": "call-click-1",
            "input_sha256": hashlib.sha256(encoded_input).hexdigest(),
            "decision": "allow",
        }) + "\n",
        encoding="utf-8",
    )


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
            home = Path(kwargs["env"]["CODEX_HOME"])
            calls.append((cmd, kwargs, tuple(sorted(home.iterdir()))))
            write_fixture_policy_ledger(
                self.attempt / "codex-tool-policy.jsonl"
            )
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
        self.assertIn("shell_tool", cmd)
        self.assertEqual(cmd[cmd.index("shell_tool") - 1], "--disable")
        self.assertIn("--dangerously-bypass-hook-trust", cmd)
        self.assertIn("hooks", cmd)
        self.assertEqual(cmd[cmd.index("hooks") - 1], "--enable")
        for feature in self.codex._NATIVE_DISABLED_TOOL_FEATURES:
            self.assertIn(feature, cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[-1], "use the app")
        self.assertNotEqual(kwargs["env"]["CODEX_HOME"], str(supplied_home))
        self.assertFalse(kwargs["text"])
        self.assertEqual(
            [path.name for path in isolated_files],
            ["auth.json", "hooks.json", "native-tool-policy.py"],
        )

        raw_path = self.attempt / "codex-events.jsonl"
        self.assertEqual(raw_path.read_text(encoding="utf-8"), stdout)
        self.assertEqual(stat.S_IMODE(raw_path.stat().st_mode), 0o600)
        trajectory_path = self.workspace / "trajectory.json"
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertEqual(trajectory["agent"]["model_name"], "gpt-5.6-sol")
        self.assertEqual(trajectory["extra"]["turn_count"], 2)
        self.assertEqual(trajectory["extra"]["source_transcript"], "codex-events.jsonl")
        self.assertEqual(
            trajectory["extra"]["tool_policy"],
            {
                "mode": "native_mcp_only",
                "allowed_mcp_servers": ["computer-use"],
                "verified": True,
            },
        )
        self.assertNotIn("clicked secret target", trajectory_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(trajectory_path.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(
                (self.attempt / "codex-tool-policy.jsonl").read_text(
                    encoding="utf-8"
                )
            )["tool_name"],
            "mcp__computer-use__click",
        )

    def test_stock_profile_keeps_workspace_write_sandbox(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.auth_home)}, clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    return_value=FakeProc(stdout=fixture_stdout()),
                ) as run:
            result = self.codex.run(
                "edit the files", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertTrue(result["completed"])
        cmd = run.call_args.args[0]
        self.assertNotIn("shell_tool", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")

    def test_native_profile_rejects_non_mcp_tool_use_and_retains_private_events(self):
        forbidden_items = (
            {"id": "shell-1", "type": "command_execution", "command": "osascript", "status": "completed"},
            {"id": "file-1", "type": "file_change", "changes": [], "status": "completed"},
            {"id": "web-1", "type": "web_search", "query": "answer"},
            {"id": "collab-1", "type": "collab_tool_call", "tool": "spawn_agent"},
            {"id": "future-1", "type": "future_tool", "arguments": {}},
        )
        for item in forbidden_items:
            with self.subTest(item_type=item["type"]):
                stdout = (
                    '{"type":"thread.started","thread_id":"thread-1"}\n'
                    + json.dumps({"type": "item.started", "item": item})
                    + "\n"
                )
                with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                        mock.patch.object(
                            self.codex.subprocess,
                            "run",
                            return_value=FakeProc(stdout=stdout),
                        ):
                    result = self.codex.run(
                        "use the app", str(self.workspace), "gpt-5.6-sol", 10
                    )

                self.assertFalse(result["completed"])
                self.assertIn("native tool policy rejected", result["error"])
                self.assertEqual(
                    (self.attempt / "codex-events.jsonl").read_text(encoding="utf-8"),
                    stdout,
                )
                self.assertEqual(
                    stat.S_IMODE((self.attempt / "codex-events.jsonl").stat().st_mode),
                    0o600,
                )
                self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_profile_rejects_other_mcp_servers(self):
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.started","item":{"id":"call-1","type":"mcp_tool_call",'
            '"server":"filesystem","tool":"write_file","status":"in_progress"}}\n'
        )
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    return_value=FakeProc(stdout=stdout),
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("rejected MCP server 'filesystem'", result["error"])
        self.assertTrue((self.attempt / "codex-events.jsonl").exists())
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_hook_allows_only_computer_use_mcp_and_writes_private_ledger(self):
        home = self.root / "hook-home"
        home.mkdir()
        ledger = self.codex._install_native_tool_policy(
            home,
            launcher=self.launcher,
        )
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        cases = (
            ("mcp__computer-use__click", None),
            ("apply_patch", "deny"),
            ("mcp__filesystem__write_file", "deny"),
        )
        for index, (tool_name, expected) in enumerate(cases):
            payload = {
                "tool_name": tool_name,
                "tool_use_id": f"call-{index}",
                "tool_input": {"secret": "private"},
            }
            proc = subprocess.run(
                command,
                shell=True,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )
            output = json.loads(proc.stdout)
            self.assertEqual(
                output["hookSpecificOutput"].get("permissionDecision"),
                expected,
            )

        records = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["decision"] for record in records],
            ["allow", "block", "block"],
        )
        self.assertNotIn("secret", ledger.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
        self.assertNotIn(
            "timeout",
            hooks["hooks"]["PreToolUse"][0]["hooks"][0],
        )

    def test_native_profile_rejects_dropped_event_warning(self):
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"id":"error-1","type":"error",'
            '"message":"in-process app-server event stream lagged; dropped 2 events"}}\n'
        )
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    return_value=FakeProc(stdout=stdout),
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("rejected incomplete event stream", result["error"])
        self.assertTrue((self.attempt / "codex-events.jsonl").exists())
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_profile_rejects_policy_ledger_argument_mismatch(self):
        def fake_run(_cmd, **_kwargs):
            ledger = self.attempt / "codex-tool-policy.jsonl"
            ledger.write_text(
                json.dumps({
                    "tool_name": "mcp__computer-use__click",
                    "tool_use_id": "call-click-1",
                    "input_sha256": "a" * 64,
                    "decision": "allow",
                }) + "\n",
                encoding="utf-8",
            )
            return FakeProc(stdout=fixture_stdout())

        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    side_effect=fake_run,
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("ledger does not match Codex MCP trajectory", result["error"])
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_profile_rejects_incomplete_mcp_lifecycle(self):
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.started","item":{"id":"item_mcp_1",'
            '"type":"mcp_tool_call","server":"computer-use","tool":"click",'
            '"arguments":{"x":420,"y":180},"status":"in_progress"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,'
            '"cached_input_tokens":0,"output_tokens":1,'
            '"reasoning_output_tokens":0}}\n'
        )

        def fake_run(_cmd, **_kwargs):
            write_fixture_policy_ledger(
                self.attempt / "codex-tool-policy.jsonl"
            )
            return FakeProc(stdout=stdout)

        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    side_effect=fake_run,
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("has incomplete lifecycle", result["error"])
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_profile_rejects_unknown_event_schema(self):
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"tool.executed","tool":"future_builtin"}\n'
        )
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    return_value=FakeProc(stdout=stdout),
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("rejected unknown event type", result["error"])
        self.assertTrue((self.attempt / "codex-events.jsonl").exists())
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_timeout_retains_and_checks_partial_private_events(self):
        stdout = (
            b'{"type":"thread.started","thread_id":"thread-1"}\n'
            b'{"type":"item.started","item":{"id":"shell-1",'
            b'"type":"command_execution","command":"osascript",'
            b'"status":"in_progress"}}\n'
        )
        timeout = subprocess.TimeoutExpired(
            ["codex"],
            10,
            output=stdout,
            stderr=b"timed out",
        )
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    side_effect=timeout,
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("timeout after 10s", result["error"])
        self.assertIn("rejected Codex item type 'command_execution'", result["error"])
        self.assertEqual(
            (self.attempt / "codex-events.jsonl").read_bytes(),
            stdout,
        )
        self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_native_timeout_retains_non_utf8_partial_bytes_exactly(self):
        stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n\xe2'
        timeout = subprocess.TimeoutExpired(
            ["codex"],
            10,
            output=stdout,
            stderr=None,
        )
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    side_effect=timeout,
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("native evidence verification failed", result["error"])
        self.assertEqual(
            (self.attempt / "codex-events.jsonl").read_bytes(),
            stdout,
        )

    def test_native_completion_retains_non_utf8_bytes_before_rejecting(self):
        stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n\xe2'
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex.subprocess,
                    "run",
                    return_value=FakeProc(stdout=stdout, stderr=b""),
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertIn("native ATIF conversion failed", result["error"])
        self.assertEqual(
            (self.attempt / "codex-events.jsonl").read_bytes(),
            stdout,
        )

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
