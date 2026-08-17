import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from obench.atif import validate_trajectory
from obench import mcp_stdio_collector


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
            "tool_name": "mcp__computer_use__click",
            "tool_use_id": "call-click-1",
            "input_sha256": hashlib.sha256(encoded_input).hexdigest(),
            "decision": "allow",
        }) + "\n",
        encoding="utf-8",
    )


def official_stdout(*, surface_kind="computerUse", include_surface=True):
    meta = {"codex/nodeReplExecutionDurationMs": 12.5}
    if include_surface:
        meta["codex/toolSurface"] = {"kind": surface_kind}
    events = (
        {"type": "thread.started", "thread_id": "thread-official"},
        {
            "type": "item.started",
            "item": {
                "id": "node-call-1",
                "type": "mcp_tool_call",
                "server": "node_repl",
                "tool": "js",
                "arguments": {"code": "await sky.get_app_state({app: 'Fixture'})"},
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "node-call-1",
                "type": "mcp_tool_call",
                "server": "node_repl",
                "tool": "js",
                "arguments": {"code": "await sky.get_app_state({app: 'Fixture'})"},
                "result": {"content": [{"type": "text", "text": "state"}], "_meta": meta},
                "status": "completed",
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 4,
                "reasoning_output_tokens": 1,
            },
        },
    )
    return "".join(json.dumps(event) + "\n" for event in events)


def write_official_policy_ledger(path):
    arguments = {"code": "await sky.get_app_state({app: 'Fixture'})"}
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    path.write_text(
        json.dumps({
            "tool_name": "mcp__node_repl__js",
            "tool_use_id": "node-call-1",
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
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
        self.node_repl = self.root / "Codex.app" / "node_repl"
        self.node_repl.parent.mkdir()
        self.node_repl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.node_repl.chmod(0o700)
        self.node_modules = self.root / "Codex.app" / "node_modules"
        self.node_modules.mkdir()
        self.skill_path = self.root / "computer-use" / "SKILL.md"
        self.skill_path.parent.mkdir()
        self.skill_path.write_text(
            "# Computer Use\nUse `@oai/sky` through node_repl.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def native_env(self):
        return {
            "CODEX_HOME": str(self.auth_home),
            "CUB_MCP_COMMAND": str(self.launcher),
            "OPENBENCH_NATIVE_TRIAL_ID": "trial-1",
            "OPENBENCH_NATIVE_MCP_LEDGER": str(self.attempt / "mcp-ledger.jsonl"),
            "OPENBENCH_NATIVE_MCP_ALLOWED_TOOLS": '["click"]',
            "OPENBENCH_NATIVE_MCP_ARGUMENT_POLICY": (
                '{"forbid_focus_change":true,"forbid_global_delivery":true}'
            ),
            "OPENBENCH_NATIVE_MCP_CALL_CONTRACT": (
                '[{"tool":"click","required_arguments":{"include_state":true}}]'
            ),
            "OPENBENCH_NATIVE_MCP_STATE_RESPONSE_MODE": "auto",
        }

    def official_env(self):
        return {
            "CODEX_HOME": str(self.auth_home),
            "OPENBENCH_NATIVE_COMPUTER_USE_PROFILE": "official_codex",
            "OPENBENCH_NATIVE_CODEX_NODE_REPL_COMMAND": str(self.node_repl),
            "OPENBENCH_NATIVE_CODEX_NODE_MODULE_DIRS": str(self.node_modules),
            "OPENBENCH_NATIVE_CODEX_SKILL_PATH": str(self.skill_path),
            "OPENBENCH_NATIVE_EVIDENCE_DIR": str(self.attempt),
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
        cmd, kwargs = calls[0]
        self.assertNotIn("start_new_session", kwargs)
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
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(self.auth_home),
                    "HOME": str(self.root),
                    "PATH": os.environ.get("PATH", ""),
                    "HOST_API_KEY": "must-not-reach-native-child",
                },
                clear=True,
            ),
            mock.patch.object(self.codex, "_run_native_command", side_effect=fake_run),
        ):
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
        self.assertIn(
            "mcp_servers.computer-use.env_vars="
            + json.dumps(
                list(self.codex._NATIVE_MCP_ENV_VARS),
                separators=(",", ":"),
            ),
            overrides,
        )
        self.assertIn(
            'mcp_servers.computer-use.enabled_tools=["click"]',
            overrides,
        )
        self.assertIn("mcp_servers.computer-use.enabled=true", overrides)
        self.assertIn("mcp_servers.computer-use.required=true", overrides)
        self.assertIn('model_reasoning_effort="medium"', overrides)
        self.assertIn('service_tier="default"', overrides)
        self.assertIn("shell_tool", cmd)
        self.assertEqual(cmd[cmd.index("shell_tool") - 1], "--disable")
        self.assertIn("--dangerously-bypass-hook-trust", cmd)
        self.assertIn("hooks", cmd)
        self.assertEqual(cmd[cmd.index("hooks") - 1], "--enable")
        for feature in self.codex._NATIVE_DISABLED_TOOL_FEATURES:
            self.assertIn(feature, cmd)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("-s", cmd)
        self.assertEqual(cmd[-1], "use the app")
        self.assertNotEqual(kwargs["env"]["CODEX_HOME"], str(supplied_home))
        self.assertNotIn("HOST_API_KEY", kwargs["env"])
        self.assertEqual(
            kwargs["env"]["OPENBENCH_NATIVE_TRIAL_ID"],
            "trial-1",
        )
        self.assertEqual(
            kwargs["env"]["OPENBENCH_NATIVE_MCP_STATE_RESPONSE_MODE"],
            "auto",
        )
        self.assertIn(
            "required_arguments",
            kwargs["env"]["OPENBENCH_NATIVE_MCP_CALL_CONTRACT"],
        )
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
                "allowed_tools": ["click"],
                "blocked_attempt_count": 0,
                "blocked_tools": [],
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
            "mcp__computer_use__click",
        )

    def test_native_profile_records_blocked_tool_attempt_without_failing(self):
        def fake_run(_cmd, **_kwargs):
            ledger = self.attempt / "codex-tool-policy.jsonl"
            write_fixture_policy_ledger(ledger)
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "tool_name": "apply_patch",
                    "tool_use_id": "blocked-apply-patch",
                    "input_sha256": hashlib.sha256(b"{}").hexdigest(),
                    "decision": "block",
                }) + "\n")
            return FakeProc(stdout=fixture_stdout())

        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex,
                    "_run_native_command",
                    side_effect=fake_run,
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertTrue(result["completed"], result)
        trajectory = json.loads(
            (self.workspace / "trajectory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            trajectory["extra"]["tool_policy"]["blocked_attempt_count"], 1
        )
        self.assertEqual(
            trajectory["extra"]["tool_policy"]["blocked_tools"],
            ["apply_patch"],
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
                            self.codex,
                            "_run_native_command",
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
                    self.codex,
                    "_run_native_command",
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
            allowed_tools=("click",),
            argument_policy={
                "forbid_focus_change": True,
                "forbid_global_delivery": True,
            },
            call_contract=(),
        )
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        cases = (
            ("mcp__computer_use__click", {"secret": "private"}, "allow"),
            (
                "mcp__computer_use__click",
                {"allow_focus_change": True},
                "deny",
            ),
            (
                "mcp__computer_use__click",
                {"allow_global_cursor": True},
                "deny",
            ),
            (
                "mcp__computer_use__click",
                {"activate": True},
                "deny",
            ),
            (
                "mcp__computer_use__click",
                {"allow_global_keyboard": True},
                "deny",
            ),
            ("mcp__computer_use__open_url", {}, "deny"),
            ("apply_patch", {}, "deny"),
            ("mcp__filesystem__write_file", {}, "deny"),
        )
        for index, (tool_name, tool_input, expected) in enumerate(cases):
            payload = {
                "tool_name": tool_name,
                "tool_use_id": f"call-{index}",
                "tool_input": tool_input,
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
            [
                "allow",
                "block",
                "block",
                "block",
                "block",
                "block",
                "block",
                "block",
            ],
        )
        self.assertNotIn("secret", ledger.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
        self.assertNotIn(
            "timeout",
            hooks["hooks"]["PreToolUse"][0]["hooks"][0],
        )

    def test_native_hook_enforces_call_contract_without_advancing_on_blocks(self):
        home = self.root / "contract-hook-home"
        home.mkdir()
        contract = (
            {
                "tool": "click",
                "required_arguments": {
                    "app": "org.openbench.fixture",
                    "element_id": "e1@s1",
                    "include_state": False,
                },
            },
            {
                "tool": "get_app_state",
                "required_arguments": {
                    "app": "org.openbench.fixture",
                    "include_screenshot": False,
                },
            },
        )
        ledger = self.codex._install_native_tool_policy(
            home,
            launcher=self.launcher,
            allowed_tools=("click", "get_app_state"),
            argument_policy={
                "forbid_focus_change": True,
                "forbid_global_delivery": True,
            },
            call_contract=contract,
        )
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        cases = (
            (
                "wrong-tool-before-first",
                "mcp__computer_use__get_app_state",
                {"app": "org.openbench.fixture", "include_screenshot": False},
                "deny",
            ),
            (
                "wrong-args-before-first",
                "mcp__computer_use__click",
                {
                    "app": "org.openbench.fixture",
                    "element_id": "wrong",
                    "include_state": False,
                },
                "deny",
            ),
            (
                "extra-args-before-first",
                "mcp__computer_use__click",
                {
                    **contract[0]["required_arguments"],
                    "mouse_button": "right",
                },
                "deny",
            ),
            (
                "exact-first-after-blocks",
                "mcp__computer_use__click",
                contract[0]["required_arguments"],
                "allow",
            ),
            (
                "wrong-args-before-second",
                "mcp__computer_use__get_app_state",
                {"app": "wrong", "include_screenshot": False},
                "deny",
            ),
            (
                "exact-second-after-block",
                "mcp__computer_use__get_app_state",
                contract[1]["required_arguments"],
                "allow",
            ),
            (
                "extra-after-contract",
                "mcp__computer_use__get_app_state",
                contract[1]["required_arguments"],
                "deny",
            ),
        )
        for use_id, tool_name, tool_input, expected in cases:
            proc = subprocess.run(
                command,
                shell=True,
                input=json.dumps({
                    "tool_name": tool_name,
                    "tool_use_id": use_id,
                    "tool_input": tool_input,
                }),
                text=True,
                capture_output=True,
                check=True,
            )
            output = json.loads(proc.stdout)
            self.assertEqual(
                output["hookSpecificOutput"]["permissionDecision"],
                expected,
                use_id,
            )

        records = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["decision"] for record in records],
            ["block", "block", "block", "allow", "block", "allow", "block"],
        )
        self.assertEqual(
            [
                record["tool_use_id"]
                for record in records
                if record["decision"] == "allow"
            ],
            ["exact-first-after-blocks", "exact-second-after-block"],
        )
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

    def test_native_profile_rejects_dropped_event_warning(self):
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"id":"error-1","type":"error",'
            '"message":"in-process app-server event stream lagged; dropped 2 events"}}\n'
        )
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex,
                    "_run_native_command",
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
                    "tool_name": "mcp__computer_use__click",
                    "tool_use_id": "call-click-1",
                    "input_sha256": "a" * 64,
                    "decision": "allow",
                }) + "\n",
                encoding="utf-8",
            )
            return FakeProc(stdout=fixture_stdout())

        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex,
                    "_run_native_command",
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
                    self.codex,
                    "_run_native_command",
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
                    self.codex,
                    "_run_native_command",
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
                    self.codex,
                    "_run_native_command",
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

    def test_native_timeout_writes_atif_for_valid_completed_events(self):
        stdout = fixture_stdout().encode("utf-8")
        timeout = subprocess.TimeoutExpired(
            ["codex"],
            10,
            output=stdout,
            stderr=b"timed out",
        )

        def time_out(*args, **kwargs):
            write_fixture_policy_ledger(
                self.attempt / "codex-tool-policy.jsonl"
            )
            raise timeout

        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex,
                    "_run_native_command",
                    side_effect=time_out,
                ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 10
            )

        self.assertFalse(result["completed"])
        self.assertEqual(result["terminal_status"], "timeout")
        trajectory = json.loads(
            (self.workspace / "trajectory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertEqual(
            trajectory["final_metrics"]["total_completion_tokens"],
            7,
        )

    def test_native_timeout_terminates_process_group_and_seals_collector(self):
        server_pid_path = self.root / "hung-server.pid"
        server_script = self.root / "hung-server.py"
        server_script.write_text(
            "import os\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            f"Path({str(server_pid_path)!r}).write_text(str(os.getpid()))\n"
            "sys.stdin.buffer.read()\n"
            "while True:\n"
            "    time.sleep(1)\n",
            encoding="utf-8",
        )
        package_root = Path(__file__).parents[2]
        self.launcher.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(package_root)!r})\n"
            "from obench.native_run import collector_main\n"
            "raise SystemExit(collector_main())\n",
            encoding="utf-8",
        )
        self.launcher.chmod(0o700)
        owner_path = self.attempt / "mcp-process-owner.json"
        env = self.native_env() | {
            "OPENBENCH_NATIVE_MCP_SERVER_COMMAND": json.dumps(
                [sys.executable, str(server_script)]
            ),
            "OPENBENCH_NATIVE_MCP_COLLECTOR_RUN_ID": "collector-timeout",
            "OPENBENCH_NATIVE_MCP_OWNER_PATH": str(owner_path),
        }
        expected_policy = self.root / "expected-policy.jsonl"
        write_fixture_policy_ledger(expected_policy)
        parent_script = self.root / "codex-parent.py"
        parent_script.write_text(
            f"""import subprocess
import sys
import time
from pathlib import Path

subprocess.Popen(
    [{str(self.launcher)!r}],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 3
while not Path({str(owner_path)!r}).exists() or not Path({str(server_pid_path)!r}).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("collector topology did not start")
    time.sleep(0.01)
Path({str(self.attempt / "codex-tool-policy.jsonl")!r}).write_text(
    {expected_policy.read_text(encoding="utf-8")!r},
    encoding="utf-8",
)
sys.stdout.buffer.write({fixture_stdout().encode("utf-8")!r})
sys.stdout.buffer.flush()
time.sleep(60)
""",
            encoding="utf-8",
        )

        real_popen = subprocess.Popen
        popen_calls = []

        def launch_fixture(_cmd, **kwargs):
            popen_calls.append(kwargs)
            fixture_process = real_popen(
                [sys.executable, str(parent_script)], **kwargs
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if owner_path.exists() and server_pid_path.exists():
                    return fixture_process
                time.sleep(0.01)
            os.killpg(fixture_process.pid, signal.SIGKILL)
            fixture_process.wait()
            self.fail("collector topology did not start")

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                self.codex.subprocess,
                "Popen",
                side_effect=launch_fixture,
            ),
        ):
            result = self.codex.run(
                "use the app", str(self.workspace), "gpt-5.6-sol", 0.2
            )

        self.assertFalse(result["completed"])
        self.assertEqual(result["terminal_status"], "timeout")
        self.assertTrue(popen_calls[0]["start_new_session"])
        self.assertGreater(
            self.codex._NATIVE_TERMINATE_GRACE_S,
            mcp_stdio_collector.CHILD_SHUTDOWN_GRACE_S,
        )

        server_pid = int(server_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(server_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail(f"detached MCP server {server_pid} survived native timeout")

        ledger_path = self.attempt / "mcp-ledger.jsonl"
        verified = mcp_stdio_collector.verify_ledger(ledger_path)
        self.assertFalse(verified.integrity_ok)
        self.assertEqual(verified.summary["returncode"], -9)
        self.assertEqual(verified.summary["relay_failures"], 1)
        ledger_rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(ledger_rows[-1]["record_type"], "ledger_seal")

        trajectory = json.loads(
            (self.workspace / "trajectory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertNotIn("native evidence verification failed", result["error"])

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
                    self.codex,
                    "_run_native_command",
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
                    self.codex,
                    "_run_native_command",
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
                mock.patch.object(self.codex, "_run_native_command") as run:
            result = self.codex.run("do it", str(self.workspace), "gpt-5.5-medium", 10)
        self.assertFalse(result["completed"])
        self.assertTrue(result["startup_failure"])
        self.assertIn("model must be 'gpt-5.6-sol'", result["error"])
        run.assert_not_called()

    def test_native_profile_keeps_raw_jsonl_and_fails_closed_on_invalid_source(self):
        stdout = '{"type":"thread.started","thread_id":"thread-1"}\nnot-json\n'
        with mock.patch.dict(os.environ, self.native_env(), clear=True), \
                mock.patch.object(
                    self.codex,
                    "_run_native_command",
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
                    mock.patch.object(self.codex, "_run_native_command") as run:
                result = self.codex.run("do it", str(self.workspace), "gpt-5.6-sol", 10)
            self.assertFalse(result["completed"])
            self.assertTrue(result["startup_failure"])
            self.assertIn("absolute executable file", result["error"])
            run.assert_not_called()

    def test_official_profile_configures_only_node_repl_and_writes_evidence(self):
        calls = []
        stdout = official_stdout()

        def fake_run(cmd, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            calls.append((cmd, kwargs, tuple(sorted(path.name for path in home.iterdir()))))
            write_official_policy_ledger(
                self.attempt / "codex-tool-policy.jsonl"
            )
            proc = FakeProc(stdout=stdout.encode("utf-8"), stderr=b"")
            proc.computer_use_event_timings = [{
                "item_id": "node-call-1",
                "request_unix_ns": 100,
                "response_unix_ns": 200,
            }]
            return proc

        with (
            mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(self.auth_home),
                    "HOME": str(self.root),
                    "PATH": os.environ.get("PATH", ""),
                },
                clear=True,
            ),
            mock.patch.object(
                self.codex,
                "_run_official_native_command",
                side_effect=fake_run,
            ),
        ):
            result = self.codex.run(
                "inspect the fixture",
                str(self.workspace),
                "gpt-5.6-sol",
                10,
                env_override=self.official_env(),
            )

        self.assertTrue(result["completed"], result)
        self.assertEqual(
            result["computer_use_event_timings"],
            [{
                "item_id": "node-call-1",
                "request_unix_ns": 100,
                "response_unix_ns": 200,
            }],
        )
        cmd, kwargs, isolated_files = calls[0]
        overrides = [cmd[index + 1] for index, arg in enumerate(cmd[:-1]) if arg == "-c"]
        self.assertIn(
            "mcp_servers.node_repl.command=" + json.dumps(str(self.node_repl)),
            overrides,
        )
        self.assertIn("mcp_servers.node_repl.args=[]", overrides)
        self.assertIn(
            "mcp_servers.node_repl.env.NODE_REPL_NODE_MODULE_DIRS="
            + json.dumps(str(self.node_modules)),
            overrides,
        )
        self.assertIn(
            "mcp_servers.node_repl.env.NODE_REPL_TRUSTED_CODE_PATHS="
            + json.dumps(str(self.node_modules)),
            overrides,
        )
        self.assertIn('mcp_servers.node_repl.enabled_tools=["js"]', overrides)
        self.assertIn("mcp_servers.node_repl.enabled=true", overrides)
        self.assertIn("mcp_servers.node_repl.required=true", overrides)
        self.assertFalse(any("mcp_servers.computer-use" in value for value in overrides))
        self.assertFalse(any("CUB_MCP_COMMAND" in value for value in overrides))
        self.assertNotIn("OPENBENCH_NATIVE_CODEX_NODE_REPL_COMMAND", kwargs["env"])
        self.assertNotIn("OPENBENCH_NATIVE_EVIDENCE_DIR", kwargs["env"])
        self.assertEqual(
            isolated_files,
            ("auth.json", "hooks.json", "native-tool-policy.py"),
        )
        self.assertIn("@oai/sky", cmd[-1])
        self.assertIn("inspect the fixture", cmd[-1])
        self.assertIn("combined with the Computer Use operation", cmd[-1])
        self.assertEqual(
            (self.attempt / "codex-events.jsonl").read_text(encoding="utf-8"),
            stdout,
        )
        trajectory = json.loads(
            (self.workspace / "trajectory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertEqual(
            trajectory["extra"]["tool_policy"],
            {
                "mode": "official_codex_node_repl_only",
                "allowed_mcp_servers": ["node_repl"],
                "allowed_tools": ["js"],
                "tool_surface": "mcp__node_repl__js",
                "required_result_metadata": "codex/toolSurface.kind=computerUse",
                "blocked_attempt_count": 0,
                "blocked_tools": [],
                "verified": True,
            },
        )

    def test_official_hook_allows_only_node_repl_js(self):
        home = self.root / "official-hook-home"
        home.mkdir()
        ledger = self.codex._install_official_tool_policy(home, self.attempt)
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        cases = (
            ("mcp__node_repl__js", {"code": "await sky.click({})"}, "allow"),
            ("mcp__node_repl__reset", {}, "deny"),
            ("mcp__computer_use__click", {}, "deny"),
            ("apply_patch", {}, "deny"),
        )
        for index, (tool_name, tool_input, expected) in enumerate(cases):
            proc = subprocess.run(
                command,
                shell=True,
                input=json.dumps({
                    "tool_name": tool_name,
                    "tool_use_id": f"official-{index}",
                    "tool_input": tool_input,
                }),
                text=True,
                capture_output=True,
                check=True,
            )
            output = json.loads(proc.stdout)
            self.assertEqual(
                output["hookSpecificOutput"]["permissionDecision"],
                expected,
            )
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(
            [record["decision"] for record in records],
            ["allow", "block", "block", "block"],
        )
        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)

    def test_official_profile_fails_closed_without_computer_use_result_metadata(self):
        for include_surface, surface_kind in ((False, "computerUse"), (True, "browserUse")):
            with self.subTest(
                include_surface=include_surface,
                surface_kind=surface_kind,
            ):
                stdout = official_stdout(
                    include_surface=include_surface,
                    surface_kind=surface_kind,
                )

                def fake_run(_cmd, **_kwargs):
                    write_official_policy_ledger(
                        self.attempt / "codex-tool-policy.jsonl"
                    )
                    return FakeProc(stdout=stdout.encode("utf-8"), stderr=b"")

                with (
                    mock.patch.dict(os.environ, self.official_env(), clear=True),
                    mock.patch.object(
                        self.codex,
                        "_run_official_native_command",
                        side_effect=fake_run,
                    ),
                ):
                    result = self.codex.run(
                        "use the app",
                        str(self.workspace),
                        "gpt-5.6-sol",
                        10,
                    )

                self.assertFalse(result["completed"])
                self.assertIn("did not prove", result["error"])
                self.assertTrue((self.attempt / "codex-events.jsonl").exists())
                self.assertFalse((self.workspace / "trajectory.json").exists())

    def test_official_streaming_runner_captures_response_timing_without_rewriting(self):
        event = next(
            line for line in official_stdout().splitlines()
            if json.loads(line).get("type") == "item.completed"
        )
        emitter = self.root / "emit-official-event.py"
        emitter.write_text(
            "import sys\n"
            f"sys.stdout.buffer.write({(event + chr(10)).encode('utf-8')!r})\n"
            "sys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        completed = self.codex._run_official_native_command(
            [sys.executable, str(emitter)],
            cwd=self.workspace,
            capture_output=True,
            text=False,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        self.assertEqual(completed.stdout, (event + "\n").encode("utf-8"))
        self.assertEqual(len(completed.computer_use_event_timings), 1)
        timing = completed.computer_use_event_timings[0]
        self.assertEqual(timing["item_id"], "node-call-1")
        self.assertEqual(
            timing["response_unix_ns"] - timing["request_unix_ns"],
            12_500_000,
        )

    def test_official_profile_rejects_mixed_or_incomplete_configuration(self):
        cases = (
            self.official_env() | {"CUB_MCP_COMMAND": str(self.launcher)},
            self.official_env() | {
                "OPENBENCH_NATIVE_CODEX_NODE_REPL_COMMAND": "relative"
            },
            self.official_env() | {
                "OPENBENCH_NATIVE_CODEX_SKILL_PATH": str(self.root / "missing")
            },
            self.official_env() | {
                "OPENBENCH_NATIVE_COMPUTER_USE_PROFILE": "unknown"
            },
        )
        for env in cases:
            with (
                self.subTest(env=env),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(self.codex, "_run_official_native_command") as run,
            ):
                result = self.codex.run(
                    "use the app", str(self.workspace), "gpt-5.6-sol", 10
                )
            self.assertFalse(result["completed"])
            self.assertTrue(result["startup_failure"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
