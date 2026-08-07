from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from obench.native_macos import (
    AppEvidence,
    FocusEvent,
    FocusViolation,
    LeaseUnavailableError,
    PreflightCheck,
    PreflightResult,
    SubprocessPhaseRunner,
    WholeRunLease,
)
from obench.native_run import (
    NativeRunError,
    NativeRunHooks,
    _content_bound_command_digest,
    _harness_version_matches,
    _inspect_setup_app_identity,
    _inspect_setup_app_process,
    _managed_proxy,
    _mcp_serve_owners,
    _recheck_process_identity,
    _require_setup_app,
    _require_setup_processes,
    load_config,
    run_native,
)
from obench.native_trial import NativeTrialError, load_native_trial


HEX_A = "a" * 64
PROCESS_START_TOKEN = "Fri Aug 7 12:00:00 2026"


class FakeFocusMonitor:
    def __init__(
        self,
        bundle_id="com.openbench.fixture",
        activity=None,
        pid=123,
    ):
        self.started = False
        self.stopped = False
        self.violations = ()
        self.bundle_id = bundle_id
        self.pid = pid
        self.events = ()
        self.activity = activity

    def start(self):
        if self.activity is not None:
            self.activity.append("focus:start")
        self.started = True
        self.events = (FocusEvent(
            self.bundle_id,
            "Fixture",
            self.pid,
            0.0,
            datetime.now(timezone.utc).isoformat(),
        ),)

    def stop(self):
        if self.activity is not None:
            self.activity.append("focus:stop")
        self.stopped = True
        self.events = self.events + (FocusEvent(
            self.bundle_id,
            "Fixture",
            self.pid,
            1.0,
            datetime.now(timezone.utc).isoformat(),
        ),)


class FakeMcpOwnerMonitor:
    def __init__(self, command, probe, activity=None):
        self.command = command
        self.probe = probe
        self.activity = activity
        self.samples = ()

    def _sample(self):
        owners = tuple(self.probe(self.command))
        sample = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "observed_at_monotonic": 0.0,
            "unrelated_serve_pids": sorted(owner["pid"] for owner in owners),
        }
        self.samples = self.samples + (sample,)

    def start(self):
        if self.activity is not None:
            self.activity.append("owner:start")
        self._sample()

    def stop(self):
        self._sample()
        if self.activity is not None:
            self.activity.append("owner:stop")
        if any(sample["unrelated_serve_pids"] for sample in self.samples):
            raise NativeRunError(
                "unrelated computer-use-mcp serve owner appeared during agent phase"
            )


class FakeAdapter:
    def __init__(self, *, fail=False, retry_once=False, tool="click", activity=None):
        self.fail = fail
        self.retry_once = retry_once
        self.calls = 0
        self.tool = tool
        self.activity = activity

    def run(self, instruction, workdir, model, timeout_s):
        if self.activity is not None:
            self.activity.append("agent:run")
        self.calls += 1
        if self.retry_once and self.calls == 1:
            return {
                "completed": False,
                "error": "transient startup",
                "startup_failure": True,
                "tokens": 0,
            }
        workspace = Path(workdir)
        request = {
            "jsonrpc": "2.0",
            "id": self.calls,
            "method": "tools/call",
            "params": {"name": self.tool, "arguments": {"target": "fixture"}},
        }
        launcher = os.environ["CUB_MCP_COMMAND"]
        completed = subprocess.run(
            [launcher],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            return {"completed": False, "error": completed.stderr}
        trajectory = {
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "replaced", "version": "replaced"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "Complete the fixture task."},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "Fixture task completed.",
                    "metrics": {
                        "prompt_tokens": 10,
                        "cached_tokens": 2,
                        "completion_tokens": 3,
                        "extra": {"private": "do-not-publish"},
                    },
                },
            ],
            "final_metrics": {
                "total_steps": 2,
                "total_prompt_tokens": 10,
                "total_cached_tokens": 2,
                "total_completion_tokens": 3,
            },
        }
        (workspace / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
        (workspace / "state.json").write_text(
            json.dumps({"saved": True}), encoding="utf-8"
        )
        return {
            "completed": not self.fail,
            "error": "agent failed" if self.fail else None,
            "tokens": 10,
        }


class NativeRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native_run_test_")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "instruction.md").write_text(
            "Complete the fixture task.\n", encoding="utf-8"
        )
        self.phase_script = self.root / "phase.py"
        self.oracle_path = self.root / "oracle.txt"
        self.oracle_path.write_text("oracle-v1\n", encoding="utf-8")
        self.phase_script.write_text(
            textwrap.dedent(
                """
                import json
                from pathlib import Path
                import sys

                phase = sys.argv[1]
                workspace = Path.cwd()
                if (workspace / ("fail-" + phase)).exists():
                    raise SystemExit(17)
                if phase == "setup":
                    (workspace / "setup.log").write_text("setup complete\\n")
                if phase == "verifier":
                    (workspace / "verdict.json").write_text(
                        json.dumps({"score": 1.0, "checker_exit": 0})
                    )
                    if (workspace / "mutate-oracle").exists():
                        Path(__file__).with_name("oracle.txt").write_text("changed")
                if phase == "reset":
                    path = workspace / "reset.log"
                    path.write_text(path.read_text() + "reset\\n" if path.exists() else "reset\\n")
                    if (workspace / "destructive-reset").exists():
                        (workspace / "state.json").write_text(json.dumps({"saved": False}))
                        (workspace / "verdict.json").write_text("destroyed")
                        (workspace / "trajectory.json").write_text("destroyed")
                """
            ),
            encoding="utf-8",
        )
        self.mcp_script = self.root / "mcp.py"
        self.mcp_script.write_text(
            textwrap.dedent(
                """
                import json
                import sys

                for line in sys.stdin:
                    request = json.loads(line)
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "content": [],
                            "isError": False,
                            "_meta": {
                                "computer-use-mcp/delivery": {
                                    "delivery_tier": "tier1-ax-attribute",
                                    "fallback_reasons": [],
                                }
                            },
                        },
                    }
                    print(json.dumps(response), flush=True)
                """
            ),
            encoding="utf-8",
        )
        self.config_path = self.root / "native.toml"
        self._write_config()

    def tearDown(self):
        self.temp.cleanup()

    def _write_config(self):
        command = json.dumps([sys.executable, str(self.mcp_script)])
        phase = json.dumps([sys.executable, str(self.phase_script), "PHASE"])
        content = f'''\
schema_version = "openbench.native-run.v0"
trial_id = "native-fixture-trial1"
output_dir = "bundle"
results_path = "results.jsonl"
lease_path = "{self.root / 'lease.lock'}"
workspace = "workspace"
atif_path = "trajectory.json"
verdict_path = "verdict.json"

[task]
id = "computer-use-fixture"
instruction = "workspace/instruction.md"
verifier_oracle_paths = ["phase.py", "oracle.txt"]

[harness]
name = "fixture-harness"
version = "1.2.3"
version_source = "fixture"

[model]
name = "fixture-model"
provider = "fixture-provider"
revision = "fixture-model-2026-08-01"

[mcp]
name = "computer-use-mcp"
version = "0.9.0"
command = {command}
client_command_env = "CUB_MCP_COMMAND"
allowed_tools = ["click"]
forbidden_tools = ["delete_skill"]

[environment]
architecture = "arm64"
hardware_model = "MacFixture1,1"
mcp_bundle_id = "com.example.computer-use"

[environment.os]
version = "15.6"
build = "24G84"

[environment.app]
bundle_id = "com.openbench.fixture"
version = "1.2.3"
build = "45"
code_signature_sha256 = "{HEX_A}"

[environment.display]
width_px = 1728
height_px = 1117
scale_factor = 2.0
color_space = "Display P3"

[budget]
timeout_s = 30
max_retries = 1

[focus]
required_foreground_bundle_id = "com.openbench.fixture"
forbidden_bundle_ids = []
require_foreground_full_agent_phase = true
forbid_global_delivery = true
allowed_delivery_tiers = ["tier1-ax-attribute"]

[phases.setup]
command = {phase.replace('PHASE', 'setup')}
timeout_s = 5

[phases.verifier]
command = {phase.replace('PHASE', 'verifier')}
timeout_s = 5

[phases.reset]
command = {phase.replace('PHASE', 'reset')}
timeout_s = 5

[[artifacts]]
source = "state.json"
path = "artifacts/final-state/state.json"
media_type = "application/json"
'''
        self.config_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _preflight(passed=True):
        return PreflightResult((
            PreflightCheck(
                "screen_unlocked",
                passed,
                passed,
                True,
                "fixture session evidence",
            ),
        ))

    def _hooks(self, adapter=None, *, preflight=True):
        monitor = FakeFocusMonitor()
        hooks = NativeRunHooks(
            preflight=lambda spec, **kwargs: self._preflight(preflight),
            focus_monitor_factory=lambda allowed: monitor,
            app_probe=lambda requirement: (
                AppEvidence(
                    requirement.bundle_identifier,
                    requirement.version,
                    True,
                    "/Applications/Fixture.app",
                ),
            ),
            app_identity_probe=lambda app_path: {
                "app": str(app_path),
                "bundle_id": "com.openbench.fixture",
                "version": "1.2.3",
                "build": "45",
                "executable": str(app_path / "Contents/MacOS/Fixture"),
                "binary_sha256": "b" * 64,
                "signature_sha256": HEX_A,
                "cdhash": "d" * 40,
            },
            app_process_probe=lambda bundle_id, executable, cdhash: {
                "pid": 123,
                "executable": str(executable),
                "device": 1,
                "inode": 2,
                "cdhash": cdhash,
                "process_start_token": PROCESS_START_TOKEN,
            },
            mcp_owner_probe=lambda command: (),
            mcp_monitor_factory=lambda command, probe: FakeMcpOwnerMonitor(
                command, probe
            ),
            adapter_loader=lambda config: adapter or FakeAdapter(),
            version_probe=lambda config, loaded: "1.2.3",
        )
        return hooks, monitor

    def test_native_harness_version_accepts_resolved_binary_suffix(self):
        self.assertTrue(
            _harness_version_matches(
                "codex-cli 0.146.1",
                "codex-cli 0.146.1 (/opt/homebrew/bin/codex)",
            )
        )

    def test_native_harness_version_rejects_other_versions_and_relative_paths(self):
        self.assertFalse(
            _harness_version_matches(
                "codex-cli 0.146.1",
                "codex-cli 0.147.0 (/opt/homebrew/bin/codex)",
            )
        )
        self.assertFalse(
            _harness_version_matches(
                "codex-cli 0.146.1",
                "codex-cli 0.146.1 (codex)",
            )
        )

    def test_preflight_uses_locked_executable_and_setup_owns_target_app_start(self):
        preflight_observations = []
        app_observations = []

        def preflight(spec, *, computer_use_binary):
            preflight_observations.append({
                "apps": spec.required_apps,
                "binary": computer_use_binary,
                "setup_complete": (self.workspace / "setup.log").exists(),
            })
            return self._preflight()

        def app_probe(requirement):
            app_observations.append({
                "requirement": requirement,
                "setup_complete": (self.workspace / "setup.log").exists(),
            })
            return AppEvidence(
                requirement.bundle_identifier,
                requirement.version,
                True,
                "/Applications/Fixture.app",
            ),

        hooks, _ = self._hooks()
        hooks.preflight = preflight
        hooks.app_probe = app_probe
        run_native(self.config_path, hooks=hooks)

        self.assertEqual(len(preflight_observations), 1)
        self.assertEqual(preflight_observations[0]["apps"], ())
        self.assertFalse(preflight_observations[0]["setup_complete"])
        self.assertEqual(
            preflight_observations[0]["binary"],
            str(Path(sys.executable).resolve()),
        )
        self.assertEqual(len(app_observations), 1)
        self.assertEqual(
            app_observations[0]["requirement"].bundle_identifier,
            "com.openbench.fixture",
        )
        self.assertTrue(app_observations[0]["setup_complete"])

    def test_setup_can_materialize_missing_workspace(self):
        instruction = self.root / "instruction.md"
        instruction.write_text("Complete the fixture task.\n", encoding="utf-8")
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'instruction = "workspace/instruction.md"',
                'instruction = "instruction.md"',
            ),
            encoding="utf-8",
        )
        shutil.rmtree(self.workspace)
        original = self.phase_script.read_text(encoding="utf-8")
        self.phase_script.write_text(
            original.replace(
                'workspace = Path.cwd()',
                'workspace = Path.cwd()\n'
                'if phase == "setup" and workspace.name != "workspace":\n'
                '    workspace = workspace / "workspace"\n'
                '    workspace.mkdir()',
            ),
            encoding="utf-8",
        )

        outcome = run_native(self.config_path, hooks=self._hooks()[0])

        self.assertTrue(outcome.bundle_dir.is_dir())
        self.assertTrue((self.workspace / "setup.log").is_file())

    def test_setup_must_materialize_missing_workspace(self):
        instruction = self.root / "instruction.md"
        instruction.write_text("Complete the fixture task.\n", encoding="utf-8")
        content = self.config_path.read_text(encoding="utf-8")
        content = content.replace(
            'instruction = "workspace/instruction.md"',
            'instruction = "instruction.md"',
        ).replace(
            json.dumps([sys.executable, str(self.phase_script), "setup"]),
            json.dumps(["/usr/bin/true"]),
        )
        self.config_path.write_text(content, encoding="utf-8")
        shutil.rmtree(self.workspace)

        with self.assertRaisesRegex(
            NativeRunError,
            "setup did not materialize a regular native workspace",
        ):
            run_native(self.config_path, hooks=self._hooks()[0])

    def test_bundle_identity_matches_cub_v0_signature_contract(self):
        app = self.root / "Fixture.app"
        executable = app / "Contents/MacOS/Fixture"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fixture executable")
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump({
                "CFBundleExecutable": "Fixture",
                "CFBundleIdentifier": "com.openbench.fixture",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleVersion": "45",
            }, handle)
        designated = 'identifier "com.openbench.fixture" and anchor apple generic'
        def command_runner(command, **kwargs):
            if "-r-" in command:
                return subprocess.CompletedProcess(
                    command, 0, "", f"# designated => {designated}\n"
                )
            return subprocess.CompletedProcess(
                command, 0, "", f"CDHash={'d' * 40}\n"
            )

        identity = _inspect_setup_app_identity(
            app,
            command_runner=command_runner,
        )
        binary_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
        expected_signature = hashlib.sha256(
            designated.encode("utf-8") + b"\0" + binary_sha256.encode("ascii")
        ).hexdigest()
        self.assertEqual(identity["build"], "45")
        self.assertEqual(identity["executable"], str(executable.resolve()))
        self.assertEqual(identity["signature_sha256"], expected_signature)

    def test_process_identity_binds_loaded_executable_vnode(self):
        executable = self.root / "Fixture"
        executable.write_bytes(b"fixture executable")
        executable_stat = executable.stat()
        calls = []

        def command_runner(command, **kwargs):
            calls.append(command)
            if command[:2] == ["/usr/bin/lsappinfo", "find"]:
                return subprocess.CompletedProcess(
                    command, 0, 'ASN:0x0-0x123-"Fixture":\n', ""
                )
            if command[:2] == ["/usr/bin/lsappinfo", "info"]:
                return subprocess.CompletedProcess(command, 0, '"pid"=456\n', "")
            if command[0] == "/usr/sbin/lsof":
                output = (
                    "p456\n"
                    "ftxt\n"
                    "tREG\n"
                    f"D{hex(executable_stat.st_dev)}\n"
                    f"i{executable_stat.st_ino}\n"
                    f"n{executable}\n"
                )
                return subprocess.CompletedProcess(command, 0, output, "")
            if command[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    command, 0, f"  {PROCESS_START_TOKEN}\n", ""
                )
            self.fail(f"unexpected command: {command!r}")

        identity = _inspect_setup_app_process(
            "com.openbench.fixture",
            executable,
            "d" * 40,
            command_runner=command_runner,
            process_cdhash_probe=lambda pid: "d" * 40,
        )
        self.assertEqual(identity["pid"], 456)
        self.assertEqual(identity["inode"], executable_stat.st_ino)
        self.assertEqual(
            [command[0] for command in calls],
            [
                "/usr/bin/lsappinfo",
                "/usr/bin/lsappinfo",
                "/usr/sbin/lsof",
                "/bin/ps",
                "/usr/bin/lsappinfo",
                "/usr/bin/lsappinfo",
            ],
        )
        with self.assertRaisesRegex(
            NativeRunError, "code signature does not match inspected bundle"
        ):
            _inspect_setup_app_process(
                "com.openbench.fixture",
                executable,
                "d" * 40,
                command_runner=command_runner,
                process_cdhash_probe=lambda pid: "e" * 40,
            )

    def test_process_identity_rejects_replaced_executable_vnode(self):
        executable = self.root / "Fixture"
        executable.write_bytes(b"replacement executable")

        def command_runner(command, **kwargs):
            if command[:2] == ["/usr/bin/lsappinfo", "find"]:
                return subprocess.CompletedProcess(
                    command, 0, 'ASN:0x0-0x123-"Fixture":\n', ""
                )
            if command[:2] == ["/usr/bin/lsappinfo", "info"]:
                return subprocess.CompletedProcess(command, 0, '"pid"=456\n', "")
            if command[0] == "/usr/sbin/lsof":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "p456\nftxt\ntREG\nD0x1\ni999999\nn/old/Fixture\n",
                    "",
                )
            if command[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    command, 0, f"{PROCESS_START_TOKEN}\n", ""
                )
            self.fail(f"unexpected command: {command!r}")

        with self.assertRaisesRegex(
            NativeRunError, "process is not using the inspected executable"
        ):
            _inspect_setup_app_process(
                "com.openbench.fixture",
                executable,
                "d" * 40,
                command_runner=command_runner,
                process_cdhash_probe=lambda pid: "d" * 40,
            )

    def test_running_app_identity_path_must_match_process_evidence(self):
        hooks, monitor = self._hooks()
        original_probe = hooks.app_identity_probe
        hooks.app_identity_probe = lambda app_path: {
            **original_probe(app_path),
            "app": "/Applications/Other.app",
        }
        with self.assertRaisesRegex(
            NativeRunError, "identity path does not match process evidence"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertFalse(monitor.started)

    def test_pathless_second_running_app_remains_ambiguous(self):
        hooks, monitor = self._hooks()
        hooks.app_probe = lambda requirement: (
            AppEvidence(
                requirement.bundle_identifier,
                requirement.version,
                True,
                "/Applications/Fixture.app",
            ),
            AppEvidence(
                requirement.bundle_identifier,
                requirement.version,
                True,
                None,
            ),
        )
        with self.assertRaisesRegex(
            NativeRunError, "exactly one required target app"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertFalse(monitor.started)

    def test_running_app_executable_must_be_inside_observed_bundle(self):
        hooks, monitor = self._hooks()
        original_probe = hooks.app_identity_probe
        hooks.app_identity_probe = lambda app_path: {
            **original_probe(app_path),
            "executable": "/tmp/Fixture",
        }
        with self.assertRaisesRegex(
            NativeRunError, "executable is outside its bundle"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertFalse(monitor.started)

    def test_running_process_must_use_inspected_executable(self):
        hooks, monitor = self._hooks()
        hooks.app_process_probe = lambda bundle_id, executable, cdhash: {
            "pid": 123,
            "executable": "/Applications/Other.app/Contents/MacOS/Fixture",
            "device": 1,
            "inode": 2,
            "cdhash": cdhash,
            "process_start_token": PROCESS_START_TOKEN,
        }
        with self.assertRaisesRegex(
            NativeRunError, "process identity is malformed"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertFalse(monitor.started)

    def test_running_app_build_mismatch_fails_before_agent(self):
        hooks, monitor = self._hooks()
        original_probe = hooks.app_identity_probe
        hooks.app_identity_probe = lambda app_path: {
            **original_probe(app_path),
            "build": "44",
        }
        with self.assertRaisesRegex(
            NativeRunError, "identity does not match planned identity.*build"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertFalse(monitor.started)

    def test_running_app_signature_mismatch_fails_before_agent(self):
        hooks, monitor = self._hooks()
        original_probe = hooks.app_identity_probe
        hooks.app_identity_probe = lambda app_path: {
            **original_probe(app_path),
            "signature_sha256": "c" * 64,
        }
        with self.assertRaisesRegex(
            NativeRunError, "identity does not match planned identity.*signature"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertFalse(monitor.started)

    def test_running_fixture_must_match_exact_build_manifest_paths(self):
        manifest = {
            "schema_version": "openbench.computer-use-build.v1",
            "fixtures": {
                "computer-use-fixture": {
                    "app": "/Applications/PlannedFixture.app",
                    "bundle_id": "com.openbench.fixture",
                    "version": "1.2.3",
                    "build": "45",
                    "executable": (
                        "/Applications/PlannedFixture.app/Contents/MacOS/Fixture"
                    ),
                    "binary_sha256": "b" * 64,
                    "signature_sha256": HEX_A,
                },
            },
        }
        (self.root / "build-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)
        with self.assertRaisesRegex(
            NativeRunError, "does not match planned identity.*app"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 0)
        self.assertFalse(monitor.started)

    def test_running_fixture_matches_exact_build_manifest_identity(self):
        app = "/Applications/Fixture.app"
        manifest = {
            "schema_version": "openbench.computer-use-build.v1",
            "fixtures": {
                "computer-use-fixture": {
                    "app": app,
                    "bundle_id": "com.openbench.fixture",
                    "version": "1.2.3",
                    "build": "45",
                    "executable": f"{app}/Contents/MacOS/Fixture",
                    "binary_sha256": "b" * 64,
                    "signature_sha256": HEX_A,
                },
            },
        }
        (self.root / "build-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        hooks, _ = self._hooks()
        _require_setup_app(hooks, load_config(self.config_path))

    def test_target_manifest_cannot_override_locked_environment_identity(self):
        app = "/Applications/Fixture.app"
        manifest = {
            "schema_version": "openbench.computer-use-build.v1",
            "fixtures": {
                "computer-use-fixture": {
                    "app": app,
                    "bundle_id": "com.openbench.fixture",
                    "version": "9.9.9",
                    "build": "45",
                    "executable": f"{app}/Contents/MacOS/Fixture",
                    "binary_sha256": "b" * 64,
                    "signature_sha256": HEX_A,
                },
            },
        }
        (self.root / "build-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)

        with self.assertRaisesRegex(
            NativeRunError,
            "build manifest conflicts with locked target environment.*version",
        ):
            run_native(self.config_path, hooks=hooks)

        self.assertEqual(adapter.calls, 0)
        self.assertFalse(monitor.started)

    def test_applicable_manifest_requires_target_fixture_identity(self):
        manifest = {
            "schema_version": "openbench.computer-use-build.v1",
            "fixtures": {
                "unrelated": {
                    "app": "/Applications/Other.app",
                    "bundle_id": "org.openbench.Other",
                    "version": "1.0",
                    "build": "1",
                    "executable": (
                        "/Applications/Other.app/Contents/MacOS/Other"
                    ),
                    "binary_sha256": "b" * 64,
                    "signature_sha256": HEX_A,
                },
            },
        }
        (self.root / "build-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)

        with self.assertRaisesRegex(
            NativeRunError,
            "build manifest has no identity for 'com.openbench.fixture'",
        ):
            run_native(self.config_path, hooks=hooks)

        self.assertEqual(adapter.calls, 0)
        self.assertFalse(monitor.started)

    def test_focus_monitor_wraps_only_agent_phase(self):
        activity = []
        adapter = FakeAdapter(activity=activity)
        monitor = FakeFocusMonitor(activity=activity)
        runner = SubprocessPhaseRunner()

        class RecordingRunner:
            def run_phase(inner_self, spec):
                activity.append(f"{spec.name.value}:start")
                outcome = runner.run_phase(spec)
                activity.append(f"{spec.name.value}:end")
                return outcome

        hooks, _ = self._hooks(adapter)
        hooks.phase_runner_factory = RecordingRunner
        hooks.focus_monitor_factory = lambda allowed: monitor
        run_native(self.config_path, hooks=hooks)

        expected = [
            "setup:start",
            "setup:end",
            "focus:start",
            "agent:run",
            "focus:stop",
            "verifier:start",
            "verifier:end",
            "reset:start",
            "reset:end",
        ]
        self.assertEqual(activity, expected)

    def test_focus_monitor_excludes_proxy_lifecycle(self):
        activity = []
        adapter = FakeAdapter(activity=activity)
        monitor = FakeFocusMonitor(activity=activity)

        @contextmanager
        def managed_proxy(*args, **kwargs):
            activity.append("proxy:start")
            yield None
            activity.append("proxy:stop")

        hooks, _ = self._hooks(adapter)
        hooks.focus_monitor_factory = lambda allowed: monitor
        with patch("obench.native_run._managed_proxy", managed_proxy):
            run_native(self.config_path, hooks=hooks)

        self.assertEqual(
            activity,
            [
                "proxy:start",
                "focus:start",
                "agent:run",
                "focus:stop",
                "proxy:stop",
            ],
        )

    def test_native_proxy_persists_registered_cell_metadata(self):
        class Server:
            server_address = ("127.0.0.1", 43210)

            def __init__(self):
                self.registered = []
                self.sealed = []

            def register_cell(self, token):
                self.registered.append(token)

            def seal_cell(self, token, timeout_s):
                self.sealed.append((token, timeout_s))

            def shutdown(self):
                pass

            def server_close(self):
                pass

        class Thread:
            def join(self, timeout):
                pass

        server = Server()
        directory = self.root / "proxy-metadata"
        config = load_config(self.config_path)
        with patch(
            "obench.native_run._proxy_context",
            return_value=(server, Thread()),
        ):
            with _managed_proxy(
                config,
                directory,
                self._hooks()[0],
                "native-1",
                "codex",
            ) as context:
                self.assertEqual(context["token"], "native-1")

        metadata = json.loads(
            (directory / "native-1.meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["source"], "runner_configured")
        self.assertEqual(metadata["harness"], "codex")
        self.assertEqual(metadata["model"], "fixture-model")
        self.assertEqual(server.registered, ["native-1"])
        self.assertEqual(server.sealed, [("native-1", 5.0)])

    def test_focus_cleanup_does_not_mask_adapter_exception(self):
        class RaisingAdapter(FakeAdapter):
            def run(inner_self, instruction, workdir, model, timeout_s):
                raise RuntimeError("primary adapter failure")

        class StopFailingMonitor(FakeFocusMonitor):
            def stop(inner_self):
                super().stop()
                raise RuntimeError("focus cleanup failure")

        monitor = StopFailingMonitor()
        hooks, _ = self._hooks(RaisingAdapter())
        hooks.focus_monitor_factory = lambda allowed: monitor
        with self.assertRaisesRegex(RuntimeError, "primary adapter failure") as raised:
            run_native(self.config_path, hooks=hooks)
        self.assertRegex(
            str(raised.exception.focus_monitor_error),
            "focus cleanup failure",
        )
        self.assertTrue((self.workspace / "reset.log").is_file())

    def test_unrelated_mcp_serve_owner_fails_before_preflight_or_setup(self):
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)
        preflight_calls = []
        hooks.preflight = lambda spec, **kwargs: preflight_calls.append(spec)
        hooks.mcp_owner_probe = lambda command: ({"pid": 4312},)

        with self.assertRaisesRegex(
            NativeRunError, "unrelated computer-use-mcp serve owners.*4312"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(preflight_calls, [])
        self.assertEqual(adapter.calls, 0)
        self.assertFalse(monitor.started)
        self.assertFalse((self.workspace / "setup.log").exists())

    def test_owner_probe_covers_preflight_and_agent_boundaries(self):
        observations = []
        hooks, _ = self._hooks()

        def probe(command):
            observations.append((
                tuple(command),
                (self.workspace / "setup.log").exists(),
                list(self.root.glob("bundle.attempts/*/computer-use-mcp-collector")),
            ))
            return ()

        hooks.mcp_owner_probe = probe
        run_native(self.config_path, hooks=hooks)
        self.assertEqual(len(observations), 3)
        self.assertFalse(observations[0][1])
        self.assertEqual(observations[0][2], [])
        self.assertTrue(all(item[1] for item in observations[1:]))
        self.assertTrue(all(len(item[2]) == 1 for item in observations[1:]))

    def test_transient_serve_owner_during_agent_fails_closed(self):
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)
        observations = iter((
            (),
            ({"pid": 4312},),
            (),
        ))
        hooks.mcp_owner_probe = lambda command: next(observations)

        with self.assertRaisesRegex(
            NativeRunError, "serve owner appeared during agent phase"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(monitor.stopped)
        self.assertTrue((self.workspace / "reset.log").is_file())

    def test_mcp_owner_monitor_death_fails_closed(self):
        class DeadMonitor(FakeMcpOwnerMonitor):
            def stop(inner_self):
                raise NativeRunError("MCP owner monitor failed: probe thread died")

        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)
        hooks.mcp_monitor_factory = (
            lambda command, probe: DeadMonitor(command, probe)
        )
        with self.assertRaisesRegex(NativeRunError, "probe thread died"):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(monitor.stopped)

    def test_wrong_focus_pid_fails_before_verifier(self):
        adapter = FakeAdapter()
        hooks, _ = self._hooks(adapter)
        monitor = FakeFocusMonitor(pid=999)
        hooks.focus_monitor_factory = lambda allowed: monitor
        with self.assertRaisesRegex(
            NativeRunError, "setup-established foreground process"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(monitor.stopped)
        self.assertFalse((self.workspace / "verify.log").exists())

    def test_forbidden_focus_bundle_reports_policy_violation_not_pid_mismatch(self):
        adapter = FakeAdapter()
        hooks, _ = self._hooks(adapter)
        monitor = FakeFocusMonitor(bundle_id="com.apple.Terminal", pid=999)
        event = FocusEvent(
            "com.apple.Terminal",
            "Terminal",
            999,
            0.0,
            datetime.now(timezone.utc).isoformat(),
        )
        monitor.violations = (FocusViolation(event, "not allowed"),)
        hooks.focus_monitor_factory = lambda allowed: monitor

        with self.assertRaisesRegex(NativeRunError, "focus policy violation"):
            run_native(self.config_path, hooks=hooks)

        self.assertEqual(adapter.calls, 1)
        self.assertTrue(monitor.stopped)

    def test_target_process_swap_after_agent_fails_closed(self):
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)
        calls = 0

        def process_probe(bundle_id, executable, cdhash):
            nonlocal calls
            calls += 1
            return {
                "pid": 123 if calls == 1 else 124,
                "executable": str(executable),
                "device": 1,
                "inode": 2,
                "cdhash": cdhash,
                "process_start_token": PROCESS_START_TOKEN,
            }

        hooks.app_process_probe = process_probe
        with self.assertRaisesRegex(
            NativeRunError, "target process identity changed"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(monitor.stopped)
        self.assertFalse((self.workspace / "verify.log").exists())

    def test_target_process_pid_reuse_after_agent_fails_closed(self):
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter)
        calls = 0

        def process_probe(bundle_id, executable, cdhash):
            nonlocal calls
            calls += 1
            return {
                "pid": 123,
                "executable": str(executable),
                "device": 1,
                "inode": 2,
                "cdhash": cdhash,
                "process_start_token": (
                    PROCESS_START_TOKEN
                    if calls == 1
                    else "Fri Aug 7 12:00:01 2026"
                ),
            }

        hooks.app_process_probe = process_probe
        with self.assertRaisesRegex(
            NativeRunError, "target process identity changed"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(monitor.stopped)
        self.assertFalse((self.workspace / "verify.log").exists())

    def test_separate_foreground_identity_comes_from_build_manifest(self):
        config_text = self.config_path.read_text(encoding="utf-8").replace(
            'required_foreground_bundle_id = "com.openbench.fixture"',
            'required_foreground_bundle_id = "org.openbench.FocusGuard.v0"',
        )
        self.config_path.write_text(config_text, encoding="utf-8")
        target_app = "/Applications/Fixture.app"
        guard_app = "/Applications/FocusGuard.app"
        manifest = {
            "schema_version": "openbench.computer-use-build.v1",
            "fixtures": {
                "target": {
                    "app": target_app,
                    "bundle_id": "com.openbench.fixture",
                    "version": "1.2.3",
                    "build": "45",
                    "executable": f"{target_app}/Contents/MacOS/Fixture",
                    "binary_sha256": "b" * 64,
                    "signature_sha256": HEX_A,
                },
                "guard": {
                    "app": guard_app,
                    "bundle_id": "org.openbench.FocusGuard.v0",
                    "version": "0.0.1",
                    "build": "1",
                    "executable": f"{guard_app}/Contents/MacOS/FocusGuard",
                    "binary_sha256": "f" * 64,
                    "signature_sha256": "e" * 64,
                },
            },
        }
        (self.root / "build-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        adapter = FakeAdapter()
        hooks, _ = self._hooks(adapter)
        identities = {
            target_app: {
                "bundle_id": "com.openbench.fixture",
                "version": "1.2.3",
                "build": "45",
                "binary_sha256": "b" * 64,
                "signature_sha256": HEX_A,
                "cdhash": "d" * 40,
                "executable": f"{target_app}/Contents/MacOS/Fixture",
            },
            guard_app: {
                "bundle_id": "org.openbench.FocusGuard.v0",
                "version": "0.0.1",
                "build": "1",
                "binary_sha256": "f" * 64,
                "signature_sha256": "e" * 64,
                "cdhash": "c" * 40,
                "executable": f"{guard_app}/Contents/MacOS/FocusGuard",
            },
        }
        hooks.app_probe = lambda requirement: (
            AppEvidence(
                requirement.bundle_identifier,
                requirement.version,
                True,
                (
                    target_app
                    if requirement.bundle_identifier == "com.openbench.fixture"
                    else guard_app
                ),
            ),
        )
        hooks.app_identity_probe = lambda app_path: {
            "app": str(app_path),
            **identities[str(app_path)],
        }
        hooks.app_process_probe = lambda bundle_id, executable, cdhash: {
            "pid": 123 if bundle_id == "com.openbench.fixture" else 456,
            "executable": str(executable),
            "device": 1,
            "inode": 2 if bundle_id == "com.openbench.fixture" else 3,
            "cdhash": cdhash,
            "process_start_token": PROCESS_START_TOKEN,
        }
        hooks.focus_monitor_factory = lambda allowed: FakeFocusMonitor(
            bundle_id="org.openbench.FocusGuard.v0",
            pid=456,
        )

        observation_times = iter((
            datetime(2026, 8, 7, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 12, 0, 2, tzinfo=timezone.utc),
        ))
        hooks.clock = lambda: next(observation_times)
        target_observation, foreground_observation = (
            _require_setup_processes(hooks, load_config(self.config_path))
        )
        self.assertLess(
            target_observation[1],
            foreground_observation[1],
        )
        terminal_times = iter((
            datetime(2026, 8, 7, 12, 0, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 12, 0, 4, tzinfo=timezone.utc),
        ))
        hooks.clock = lambda: next(terminal_times)
        self.assertLess(
            _recheck_process_identity(
                hooks, target_observation[0], label="target"
            ),
            _recheck_process_identity(
                hooks, foreground_observation[0], label="foreground"
            ),
        )
        hooks.clock = lambda: datetime.now(timezone.utc)
        outcome = run_native(self.config_path, hooks=hooks)
        process_ledger = (
            outcome.bundle_dir / "process/ledger.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn('"role":"foreground"', process_ledger)
        self.assertIn('"pid":456', process_ledger)
        self.assertNotIn("/Applications/", process_ledger)

    def test_serve_owner_probe_matches_only_exact_configured_executable(self):
        executable = str(Path(sys.executable).resolve())
        completed = subprocess.CompletedProcess(
            [],
            0,
            "\n".join((
                f"4312 1 Python {executable} serve --stdio",
                f"4313 1 Python {executable} /tmp/computer-use-mcp-collector",
                "4314 1 false /usr/bin/false serve --stdio",
            )),
            "",
        )
        owners = _mcp_serve_owners(
            [executable],
            command_runner=lambda *args, **kwargs: completed,
        )
        self.assertEqual([owner["pid"] for owner in owners], [4312])

    def test_source_arm_rejects_installed_computer_use_mcp_serve_owner(self):
        source_executable = self.root / "source-arm-server"
        source_executable.write_text("source", encoding="utf-8")
        installed_executable = self.root / "installed" / "computer-use-mcp"
        installed_executable.parent.mkdir()
        installed_executable.write_text("installed", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            [],
            0,
            "\n".join((
                f"5312 1 computer-use-mcp {installed_executable} serve",
                f"5313 1 source-arm-server {source_executable} health_report --json",
                f"5314 1 unrelated-tool {self.root / 'unrelated-tool'} serve",
            )),
            "",
        )
        owners = _mcp_serve_owners(
            [str(source_executable)],
            command_runner=lambda *args, **kwargs: completed,
        )
        self.assertEqual([owner["pid"] for owner in owners], [5312])

    def test_owner_probe_handles_unquoted_spaced_installed_app_path(self):
        source_executable = self.root / "source-arm-server"
        source_executable.write_text("source", encoding="utf-8")
        installed_executable = (
            self.root
            / "Computer Use MCP.app"
            / "Contents"
            / "MacOS"
            / "computer-use-mcp"
        )
        completed = subprocess.CompletedProcess(
            [],
            0,
            f"73883 1 computer-use-mcp {installed_executable} serve\n",
            "",
        )
        owners = _mcp_serve_owners(
            [str(source_executable)],
            command_runner=lambda *args, **kwargs: completed,
        )
        self.assertEqual([owner["pid"] for owner in owners], [73883])

    def test_success_generates_imports_and_seals_exact_native_bundle(self):
        hooks, monitor = self._hooks()
        outcome = run_native(self.config_path, hooks=hooks)

        self.assertTrue(monitor.started)
        self.assertTrue(monitor.stopped)
        self.assertTrue(outcome.bundle_dir.is_dir())
        self.assertEqual(outcome.row["exec_mode"], "native_macos")
        self.assertEqual(outcome.row["success"], True)
        self.assertNotIn("harbor", json.dumps(outcome.row).lower())
        self.assertEqual(len(outcome.results_path.read_text().splitlines()), 1)
        self.assertEqual(load_native_trial(outcome.bundle_dir)["run_id"], outcome.row["run_id"])
        lock = json.loads((outcome.bundle_dir / "lock.json").read_text())
        interpreter_digest = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
        self.assertNotEqual(lock["mcp"]["server_sha256"], interpreter_digest)
        argv_only = hashlib.sha256(
            json.dumps(
                [sys.executable, str(self.phase_script), "verifier"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        task = json.loads((outcome.bundle_dir / "task/task.json").read_text())
        self.assertNotEqual(task["verifier_sha256"], argv_only)
        trajectory = json.loads(
            (outcome.bundle_dir / "agent/trajectory.json").read_text()
        )
        self.assertEqual(
            set(trajectory["steps"][1]["metrics"]),
            {"prompt_tokens", "cached_tokens", "completion_tokens"},
        )
        self.assertNotIn("do-not-publish", json.dumps(trajectory))

    def test_judged_snapshot_survives_destructive_reset(self):
        (self.workspace / "destructive-reset").touch()
        hooks, _ = self._hooks()
        outcome = run_native(self.config_path, hooks=hooks)
        captured = json.loads(
            (outcome.bundle_dir / "artifacts/final-state/state.json").read_text()
        )
        self.assertEqual(captured, {"saved": True})
        self.assertEqual(json.loads((self.workspace / "state.json").read_text()), {"saved": False})

    def test_verifier_oracle_mutation_fails_closed(self):
        (self.workspace / "mutate-oracle").touch()
        hooks, _ = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "verifier or oracle bytes changed"):
            run_native(self.config_path, hooks=hooks)

    def test_explicit_mcp_tool_policy_fails_closed(self):
        hooks, _ = self._hooks(FakeAdapter(tool="delete_skill"))
        with self.assertRaisesRegex(NativeRunError, "MCP tool policy violation"):
            run_native(self.config_path, hooks=hooks)

    def test_interpreted_module_digest_binds_module_payload(self):
        package = self.root / "digest_fixture"
        package.mkdir()
        (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        sys.path.insert(0, str(self.root))
        try:
            before = _content_bound_command_digest(
                [sys.executable, "-m", "digest_fixture"], cwd=self.root
            )
            cache = package / "__pycache__"
            cache.mkdir()
            (cache / "generated.pyc").write_bytes(b"runtime bytecode")
            with_cache = _content_bound_command_digest(
                [sys.executable, "-m", "digest_fixture"], cwd=self.root
            )
            (package / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
            after = _content_bound_command_digest(
                [sys.executable, "-m", "digest_fixture"], cwd=self.root
            )
        finally:
            sys.path.remove(str(self.root))
            sys.modules.pop("digest_fixture", None)
        self.assertEqual(before, with_cache)
        self.assertNotEqual(before, after)

    def test_unknown_mcp_policy_tool_is_rejected(self):
        content = self.config_path.read_text(encoding="utf-8").replace(
            'allowed_tools = ["click"]',
            'allowed_tools = ["future_destructive_tool"]',
        )
        self.config_path.write_text(content, encoding="utf-8")
        with self.assertRaisesRegex(NativeRunError, "unknown tools"):
            load_config(self.config_path)

    def test_whole_run_lease_conflict_prevents_runtime_work(self):
        config = load_config(self.config_path)
        adapter_calls = []
        hooks, _ = self._hooks()
        hooks.adapter_loader = lambda value: adapter_calls.append(value)
        with WholeRunLease(config.lease_path):
            with self.assertRaises(LeaseUnavailableError):
                run_native(config, hooks=hooks)
        self.assertEqual(adapter_calls, [])

    def test_locked_session_fails_before_phases_or_adapter(self):
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter, preflight=False)
        with self.assertRaisesRegex(Exception, "preflight failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 0)
        self.assertFalse(monitor.started)

    def test_setup_failure_still_resets_without_starting_focus_monitor(self):
        (self.workspace / "fail-setup").touch()
        hooks, monitor = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "setup phase failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertFalse(monitor.started)
        self.assertFalse(monitor.stopped)

    def test_setup_without_exact_running_app_fails_before_focus_and_resets(self):
        hooks, monitor = self._hooks()
        hooks.app_probe = lambda requirement: (
            AppEvidence(
                requirement.bundle_identifier,
                "0.0.0",
                True,
                "/Applications/Fixture.app",
            ),
        )
        with self.assertRaisesRegex(
            NativeRunError, "setup did not establish exactly one required target app"
        ):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "setup.log").is_file())
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertFalse(monitor.started)

    def test_agent_failure_still_resets_and_stops_monitor(self):
        hooks, monitor = self._hooks(FakeAdapter(fail=True))
        with self.assertRaisesRegex(NativeRunError, "agent failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertTrue(monitor.stopped)

    def test_verifier_failure_still_resets_and_stops_monitor(self):
        (self.workspace / "fail-verifier").touch()
        hooks, monitor = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "verifier phase failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertTrue(monitor.stopped)

    def test_reset_failure_fails_closed_after_successful_agent_and_verifier(self):
        (self.workspace / "fail-reset").touch()
        hooks, monitor = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "reset phase failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue(monitor.stopped)
        self.assertFalse((self.root / "bundle").exists())

    def test_transient_pre_token_startup_retry_retains_every_attempt(self):
        adapter = FakeAdapter(retry_once=True)
        hooks, _ = self._hooks(adapter)
        outcome = run_native(self.config_path, hooks=hooks)
        records = sorted(outcome.attempts_dir.glob("attempt*/attempt.json"))
        self.assertEqual(len(records), 2)
        first = json.loads(records[0].read_text())
        self.assertTrue(first["retry"])
        self.assertEqual(outcome.row["candidate_provenance"]["retry_count"], 1)

    def test_tampered_generated_bundle_is_rejected(self):
        hooks, _ = self._hooks()
        outcome = run_native(self.config_path, hooks=hooks)
        artifact = outcome.bundle_dir / "artifacts/final-state/state.json"
        artifact.write_text('{"saved":false}', encoding="utf-8")
        with self.assertRaises(NativeTrialError):
            load_native_trial(outcome.bundle_dir)

    def test_cli_help_and_missing_argument_smoke(self):
        repo = Path(__file__).parents[2]
        help_result = subprocess.run(
            [sys.executable, "-m", "obench", "native", "--help"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("native macOS Computer-Use", help_result.stdout)
        missing = subprocess.run(
            [sys.executable, "-m", "obench", "native", "run"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("config", missing.stderr)


if __name__ == "__main__":
    unittest.main()
