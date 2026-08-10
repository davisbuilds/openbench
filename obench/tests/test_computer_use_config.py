import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest import mock

from obench.native_matrix import (
    NativeMatrixError,
    build_native_matrix,
    canonical_bytes,
    reconcile_native_state,
    validate_native_matrix,
)
from obench.native_run import NativeRunError, load_config


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "computer-use-tasks/v0/scripts/cub_v0.py"
SCOPED_AGENT_AB_SCRIPT = (
    ROOT / "computer-use-tasks/v0/scripts/scoped_agent_ab.py"
)
SPEC = importlib.util.spec_from_file_location("cub_v0", SCRIPT)
cub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cub)
SCOPED_SPEC = importlib.util.spec_from_file_location(
    "scoped_agent_ab", SCOPED_AGENT_AB_SCRIPT
)
scoped = importlib.util.module_from_spec(SCOPED_SPEC)
assert SCOPED_SPEC.loader is not None
with mock.patch.dict(sys.modules, {"cub_v0": cub}):
    SCOPED_SPEC.loader.exec_module(scoped)


class ComputerUseConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.run_root = self.base / "runs/computer-use-v0"
        self.repo = self.base / "computer-use-mcp"
        self.installed = self.base / "Applications/Computer Use MCP.app"
        self.repo.mkdir()
        self.installed.mkdir(parents=True)
        self.request = self.base / "request.toml"
        self.request.write_text(
            "\n".join([
                f'schema_version = "{cub.CONFIG_SCHEMA}"',
                f'run_root = {json.dumps(str(self.run_root))}',
                f'computer_use_mcp_repo = {json.dumps(str(self.repo))}',
                f'installed_mcp_app = {json.dumps(str(self.installed))}',
                'source_signing_identity = "Fixture Signing Identity"',
                'codex_version = "codex-cli 0.146.1"',
                "",
            ]),
            encoding="utf-8",
        )

    @staticmethod
    def identity(app):
        name = Path(app).stem
        is_textedit = name == "TextEdit"
        is_source = "OpenBench Computer Use MCP Source" in str(app)
        is_installed = name == "Computer Use MCP"
        bundle = (
            "com.apple.TextEdit" if is_textedit else
            cub.SOURCE_MCP_BUNDLE_ID if is_source else
            cub.INSTALLED_MCP_BUNDLE_ID if is_installed else
            "org.openbench.fixture"
        )
        return {
            "app": str(app),
            "bundle_id": bundle,
            "version": cub.MCP_VERSION if (is_source or is_installed) else "0.0.1",
            "build": "1",
            "executable": str(Path(app) / "Contents/MacOS/fixture"),
            "binary_sha256": "a" * 64,
            "build_stamp_unix": 123456789,
            "designated_requirement": f'identifier "{bundle}"',
            "signature_sha256": "b" * 64,
            "adhoc": is_installed,
        }

    @staticmethod
    def content_digest(command, *, cwd, extra_paths=()):
        return cub.hashlib.sha256(canonical_bytes({
            "command": list(command),
            "extra_paths": [str(path) for path in extra_paths],
        })).hexdigest()

    def test_path_safety_rejects_user_roots_and_escape(self):
        for path in ("relative", "/", str(Path.home()), str(Path.home() / "Documents")):
            with self.subTest(path=path), self.assertRaises(cub.CubError):
                cub.safe_run_root(path)
        root = cub.safe_run_root(self.run_root)
        self.assertEqual(cub.descendant(root, "workspaces/task"), root / "workspaces/task")
        with self.assertRaises(cub.CubError):
            cub.descendant(root, "../escape")

    def test_state_response_setup_readiness_uses_basic_fixture_initial_state(self):
        workspace = cub._workspace(
            self.run_root, "auto", "state-response-ab", 1
        )
        artifacts = workspace / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "fixture-state.json").write_text(
            json.dumps({
                "fixture": "basic-controls",
                "honest_counter": 0,
                "keystroke_echo": "",
                "schema_version": 1,
                "toggle_on": False,
            }),
            encoding="utf-8",
        )
        self.assertTrue(
            cub._initial_state_ready(
                self.run_root, "auto", "state-response-ab", 1
            )
        )

    def test_standalone_script_resolves_openbench_from_outside_repo(self):
        completed = subprocess.run(
            [sys.executable, os.fspath(SCRIPT), "--help"],
            cwd=self.base,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Computer-Use Bench v0", completed.stdout)

    def test_scoped_agent_ab_builds_revisions_instead_of_accepting_apps(self):
        completed = subprocess.run(
            [sys.executable, os.fspath(SCOPED_AGENT_AB_SCRIPT), "--help"],
            cwd=self.base,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--baseline-revision", completed.stdout)
        self.assertIn("--scoped-revision", completed.stdout)
        self.assertIn("--prepare-only", completed.stdout)
        self.assertNotIn("--baseline-app", completed.stdout)
        self.assertNotIn("--scoped-app", completed.stdout)
        source = SCOPED_AGENT_AB_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('["git", "archive", "--format=tar", revision]', source)
        self.assertIn("cub._extract_revision(repo, source_revision, source_tree)", source)
        self.assertIn("_require_stable_bundle(app)", source)
        self.assertIn('build_result.get("signed")', source)
        self.assertIn('locked_state_response_mode="auto"', source)
        self.assertIn("DAEMON_BUNDLE_PATH", source)
        self.assertIn('source = "daemon-evidence.json"', source)
        prompt = (
            ROOT
            / "computer-use-tasks/v0/experiments/scoped-outcome-agent-ab/instruction.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("toggle-box", prompt)
        self.assertNotIn("state_response_mode", prompt)

    def test_scoped_agent_ab_rejects_cross_arm_daemon_contamination(self):
        with self.assertRaisesRegex(scoped.ExperimentError, "baseline emitted"):
            scoped._validate_arm_encodings("baseline", {"full": 2, "outcome": 1})
        with self.assertRaisesRegex(scoped.ExperimentError, "never exercised"):
            scoped._validate_arm_encodings("scoped", {"full": 2})
        scoped._validate_arm_encodings("baseline", {"full": 2})
        scoped._validate_arm_encodings("scoped", {"full": 2, "outcome": 1})

    def test_scoped_agent_ab_requires_spawned_daemon_identity(self):
        executable = self.base / "computer-use-mcp-bin"
        executable.write_bytes(b"exact daemon")
        observed = {
            "authenticated": True,
            "buildStamp": executable.stat().st_mtime,
            "daemonIncarnationID": "incarnation-1",
            "version": cub.MCP_VERSION,
        }
        with mock.patch.object(scoped, "_daemon_lock_owners", return_value=[4321]):
            identity = scoped._validate_daemon_identity(
                observed,
                executable=executable,
                binary_sha256=scoped._sha256(executable),
                pid=4321,
            )
        self.assertEqual(identity["incarnation_id"], "incarnation-1")
        self.assertNotIn("executable", identity)
        self.assertEqual(identity["binary_sha256"], scoped._sha256(executable))
        with (
            mock.patch.object(scoped, "_daemon_lock_owners", return_value=[9999]),
            self.assertRaisesRegex(scoped.ExperimentError, "exclusively own"),
        ):
            scoped._validate_daemon_identity(
                observed,
                executable=executable,
                binary_sha256=scoped._sha256(executable),
                pid=4321,
            )

    def test_scoped_agent_ab_starts_exact_daemon_through_launchservices(self):
        app = self.base / "OpenBench Computer Use MCP Source.app"
        executable = app / "Contents/MacOS/computer-use-mcp"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"exact daemon")
        daemon_socket = self.base / "daemon.sock"
        observed = {
            "authenticated": True,
            "buildStamp": executable.stat().st_mtime,
            "daemonIncarnationID": "incarnation-1",
            "version": cub.MCP_VERSION,
        }
        launched = subprocess.CompletedProcess([], 0, "", "")

        def launch(*args, **kwargs):
            daemon_socket.touch()
            return launched

        with (
            mock.patch.object(scoped, "DAEMON_SOCKET", daemon_socket),
            mock.patch.object(
                scoped, "_daemon_lock_owners", side_effect=[[], [4321], [4321]]
            ),
            mock.patch.object(scoped, "_daemon_hello", return_value=observed),
            mock.patch.object(scoped.subprocess, "run", side_effect=launch) as run,
        ):
            pid, identity = scoped._start_exact_daemon({
                "executable": str(executable),
                "binary_sha256": scoped._sha256(executable),
            })

        self.assertEqual(pid, 4321)
        self.assertEqual(identity["incarnation_id"], "incarnation-1")
        self.assertEqual(
            run.call_args.args[0],
            ["open", "-na", str(app), "--args", "daemon"],
        )

    def test_scoped_agent_ab_removes_only_an_unowned_unix_socket(self):
        daemon_socket = self.base / "daemon.sock"
        listener = scoped.socket.socket(scoped.socket.AF_UNIX, scoped.socket.SOCK_STREAM)
        listener.bind(str(daemon_socket))
        self.addCleanup(listener.close)
        with (
            mock.patch.object(scoped, "DAEMON_SOCKET", daemon_socket),
            mock.patch.object(scoped, "_daemon_lock_owners", return_value=[]),
        ):
            self.assertIsNone(scoped._stop_daemon())
        self.assertFalse(daemon_socket.exists())

        regular_file = self.base / "not-a-socket"
        regular_file.write_text("keep", encoding="utf-8")
        with (
            mock.patch.object(scoped, "DAEMON_SOCKET", regular_file),
            mock.patch.object(scoped, "_daemon_lock_owners", return_value=[]),
            self.assertRaisesRegex(scoped.ExperimentError, "not a Unix socket"),
        ):
            scoped._stop_daemon()
        self.assertTrue(regular_file.exists())

    def test_scoped_agent_ab_restores_runtime_app_after_daemon_cleanup_failure(self):
        runtime_app = self.base / "runtime.app"
        backup = self.base / "runtime.app.scoped-agent-ab-backup"
        runtime_app.mkdir()
        (runtime_app / "arm").write_text("experiment", encoding="utf-8")
        backup.mkdir()
        (backup / "arm").write_text("original", encoding="utf-8")
        with (
            mock.patch.object(
                scoped,
                "_stop_daemon",
                side_effect=scoped.ExperimentError("shutdown failed"),
            ),
            self.assertRaisesRegex(scoped.ExperimentError, "shutdown failed"),
        ):
            scoped._restore_runtime_app(
                runtime_app,
                backup,
                primary_error=None,
            )
        self.assertEqual(
            (runtime_app / "arm").read_text(encoding="utf-8"),
            "original",
        )
        self.assertFalse(backup.exists())

    def test_scoped_agent_ab_installs_in_place_without_replacing_authorized_app(self):
        source = self.base / "source.app"
        runtime_app = self.base / "runtime.app"
        (source / "Contents").mkdir(parents=True)
        (source / "Contents/new").write_text("new", encoding="utf-8")
        (runtime_app / "Contents").mkdir(parents=True)
        (runtime_app / "Contents/stale").write_text("stale", encoding="utf-8")
        inode = runtime_app.stat().st_ino

        scoped._install(source, runtime_app)

        self.assertEqual(runtime_app.stat().st_ino, inode)
        self.assertEqual(
            (runtime_app / "Contents/new").read_text(encoding="utf-8"),
            "new",
        )
        self.assertFalse((runtime_app / "Contents/stale").exists())

    def test_owned_process_cleanup_matches_pid_start_and_command(self):
        state = self.run_root / "runtime/processes.json"
        state.parent.mkdir(parents=True)
        record = {"pid": 4321, "start_token": "Thu Aug  6 10:00:00 2026", "command": "/tmp/fixture"}
        state.write_text(json.dumps({
            "schema_version": cub.PROCESS_SCHEMA, "processes": [record]
        }), encoding="utf-8")
        signals = []
        reads = iter((dict(record), None))
        cub.terminate_owned(
            state, identity_reader=lambda pid: next(reads, None),
            signaler=lambda pid, value: signals.append((pid, value)),
        )
        self.assertEqual(signals, [(4321, cub.signal.SIGTERM)])
        self.assertFalse(state.exists())
        cub.terminate_owned(state, signaler=lambda pid, value: self.fail("unexpected signal"))

    def test_owned_process_cleanup_refuses_reused_pid(self):
        state = self.run_root / "runtime/processes.json"
        state.parent.mkdir(parents=True)
        record = {"pid": 4321, "start_token": "old", "command": "/tmp/fixture"}
        state.write_text(json.dumps({
            "schema_version": cub.PROCESS_SCHEMA, "processes": [record]
        }), encoding="utf-8")
        with self.assertRaisesRegex(cub.CubError, "refusing to signal"):
            cub.terminate_owned(
                state,
                identity_reader=lambda pid: {**record, "start_token": "new"},
                signaler=lambda pid, value: self.fail("reused pid was signaled"),
            )
        self.assertTrue(state.exists())

    def test_reset_is_idempotent_and_confined_to_run_root(self):
        workspace = cub._workspace(
            self.run_root, "installed", "basic-controls", 1
        )
        other_workspace = cub._workspace(
            self.run_root, "installed", "basic-controls", 2
        )
        (workspace / "artifacts").mkdir(parents=True)
        (workspace / "artifacts/state.json").write_text("{}", encoding="utf-8")
        (other_workspace / "artifacts").mkdir(parents=True)
        (other_workspace / "artifacts/state.json").write_text(
            '{"trial":2}', encoding="utf-8"
        )
        other_state = cub._state_path(
            self.run_root, "installed", "basic-controls", 2
        )
        other_state.parent.mkdir(parents=True)
        other_state.write_text(
            json.dumps({"schema_version": cub.PROCESS_SCHEMA, "processes": []}),
            encoding="utf-8",
        )
        unrelated = self.base / "user-document.txt"
        unrelated.write_text("keep", encoding="utf-8")
        cub.reset_runtime(self.request, "installed", "basic-controls", 1)
        cub.reset_runtime(self.request, "installed", "basic-controls", 1)
        self.assertFalse((workspace / "artifacts").exists())
        self.assertEqual(
            (other_workspace / "artifacts/state.json").read_text(encoding="utf-8"),
            '{"trial":2}',
        )
        self.assertTrue(other_state.is_file())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_trial_index_must_be_explicitly_positive(self):
        for value in (0, -1, True, "01", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(cub.CubError):
                cub._workspace(
                    self.run_root, "installed", "basic-controls", value
                )

    def test_setup_waits_for_exact_foreground_bundle(self):
        observed = iter(("com.openai.codex", cub.FIXTURE_BUNDLES["basic-controls"]))
        cub._wait_for_frontmost(
            cub.FIXTURE_BUNDLES["basic-controls"],
            timeout_s=1,
            probe=lambda: next(observed),
        )

    def test_setup_fails_when_foreground_bundle_never_matches(self):
        with self.assertRaisesRegex(cub.CubError, "did not establish required foreground"):
            cub._wait_for_frontmost(
                cub.FIXTURE_BUNDLES["basic-controls"],
                timeout_s=0,
                probe=lambda: "com.openai.codex",
            )

    def test_setup_isolates_small_tree_profile_to_state_response_task(self):
        environments = []

        def launch(_executable, _args, environment, _log):
            environments.append(dict(environment))
            return {"pid": 4321, "start_token": "fixture", "command": "fixture"}

        with (
            mock.patch.dict(os.environ, {"COMPUTER_USE_FIXTURE_SMALL_TREE": "1"}),
            mock.patch.object(cub, "_require_state_response_fixture"),
            mock.patch.object(cub, "_launch", side_effect=launch),
            mock.patch.object(cub, "_wait_for_frontmost"),
        ):
            cub.setup(self.request, "source", "basic-controls", 1)
            cub.setup(self.request, "auto", "state-response-ab", 1)

        self.assertNotIn("COMPUTER_USE_FIXTURE_SMALL_TREE", environments[0])
        self.assertEqual(environments[1]["COMPUTER_USE_FIXTURE_SMALL_TREE"], "1")

    def test_setup_restarts_fixture_when_launch_profile_does_not_match(self):
        arm = "auto"
        task = "state-response-ab"
        trial = 1
        workspace = cub._workspace(self.run_root, arm, task, trial)
        artifacts = workspace / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "fixture-state.json").write_text(
            json.dumps({
                "fixture": "basic-controls",
                "honest_counter": 0,
                "keystroke_echo": "",
                "schema_version": 1,
                "toggle_on": False,
            }),
            encoding="utf-8",
        )
        process = {"pid": 4321, "start_token": "fixture", "command": "fixture"}
        state_path = cub._state_path(self.run_root, arm, task, trial)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({
                "launch_profile": {"fixture_small_tree": False, "task": task},
                "processes": [process],
                "schema_version": cub.PROCESS_SCHEMA,
            }),
            encoding="utf-8",
        )

        def reset(*_args):
            cub.shutil.rmtree(workspace)
            state_path.unlink()

        with (
            mock.patch.object(cub, "_require_state_response_fixture"),
            mock.patch.object(cub, "_process_identity", return_value=process),
            mock.patch.object(cub, "reset_runtime", side_effect=reset) as reset_mock,
            mock.patch.object(cub, "_launch", return_value=process),
            mock.patch.object(cub, "_wait_for_frontmost"),
        ):
            cub.setup(self.request, arm, task, trial)

        reset_mock.assert_called_once()
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["launch_profile"], cub._launch_profile(task))

    def test_state_response_fixture_requires_current_revision_and_binary(self):
        app = self.run_root / "apps/ComputerUseFixture.app"
        identity = self.identity(app)
        manifest = {
            "basic_fixture_revision": cub.BASIC_REVISION,
            "fixtures": {"basic-controls": identity},
            "schema_version": "openbench.computer-use-build.v1",
        }
        self.run_root.mkdir(parents=True)
        manifest_path = self.run_root / "build-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with mock.patch.object(cub, "_bundle_info", return_value=identity):
            self.assertEqual(
                cub._require_state_response_fixture(self.run_root), identity
            )
            manifest["basic_fixture_revision"] = "0" * 40
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(cub.CubError, "required source revision"):
                cub._require_state_response_fixture(self.run_root)

    def test_static_preflight_does_not_create_run_root(self):
        request = cub._load_request(self.request)
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=cub.CubError("not built")),
            mock.patch.object(cub, "_git_has_commit", return_value=True),
            mock.patch.object(cub.shutil, "which", return_value="/usr/local/bin/codex"),
            mock.patch.object(cub, "_run", return_value=mock.Mock(stdout="codex-cli 0.146.1\n")),
            mock.patch.object(cub.Path, "is_file", return_value=False),
        ):
            result = cub._static_preflight(request)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["matched_ready"])
        self.assertFalse(self.run_root.exists())

    def test_bundle_identity_binds_designated_requirement_and_executable_bytes(self):
        app = self.base / "Fixture.app"
        executable = app / "Contents/MacOS/fixture"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"first")
        with (app / "Contents/Info.plist").open("wb") as handle:
            cub.plistlib.dump({
                "CFBundleExecutable": "fixture",
                "CFBundleIdentifier": "org.openbench.fixture",
                "CFBundleShortVersionString": "0.0.1",
                "CFBundleVersion": "1",
            }, handle)
        codesign = [
            mock.Mock(stdout="", stderr="designated => identifier \"org.openbench.fixture\"\n"),
            mock.Mock(stdout="", stderr="Signature=adhoc\n"),
        ]
        with mock.patch.object(cub, "_run", side_effect=codesign):
            first = cub._bundle_info(app)
        executable.write_bytes(b"second")
        with mock.patch.object(cub, "_run", side_effect=codesign):
            second = cub._bundle_info(app)
        self.assertNotEqual(first["binary_sha256"], second["binary_sha256"])
        self.assertNotEqual(first["signature_sha256"], second["signature_sha256"])

    def test_build_manifest_binds_system_textedit_identity(self):
        apps = self.run_root / "apps"
        source = apps / "OpenBench Computer Use MCP Source.app"
        with mock.patch.object(
            cub,
            "_bundle_info",
            side_effect=lambda app: {"app": str(app)},
        ):
            manifest = cub._build_manifest(source, apps)

        self.assertEqual(
            manifest["fixtures"]["textedit-exact-file"]["app"],
            "/System/Applications/TextEdit.app",
        )
        self.assertEqual(
            set(manifest["fixtures"]),
            {
                "basic-controls",
                "background-control",
                "guard",
                "textedit-exact-file",
            },
        )

    def test_state_response_ab_configs_bind_mode_contract_and_identical_prompt(self):
        self.assertEqual(
            cub.CALL_CONTRACT[0]["required_arguments"]["app"],
            cub.FIXTURE_BUNDLES["state-response-ab"],
        )
        self.assertEqual(cub.CALL_CONTRACT[-1]["tool"], "type_text")
        self.assertEqual(
            [
                item["required_arguments"].get("element_id")
                for item in cub.CALL_CONTRACT[1:]
            ],
            ["e7@s1", "e6@s1", "e6@s1", "e11@s1"],
        )
        self.assertTrue(all(
            item["required_arguments"].get("app")
            == cub.FIXTURE_BUNDLES["state-response-ab"]
            for item in cub.CALL_CONTRACT
        ))
        self.assertEqual(
            cub.CALL_CONTRACT[-1]["required_arguments"]["text"],
            "openbench-42",
        )
        host = {
            "os_version": "15.6", "os_build": "24G84",
            "architecture": "arm64", "hardware": "MacFixture1,1",
            "display_width": 1512, "display_height": 982,
            "display_scale": 2.0, "display_color_space": "Color LCD",
        }
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": False, "state_ab_ready": True, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            mock.patch.object(
                cub, "_content_bound_command_digest", side_effect=self.content_digest
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cub.generate(self.request, "state-ab", None, None, repetitions=2),
                0,
            )
        manifest = json.loads(
            (self.run_root / "configs/state-ab/manifest.json").read_text()
        )
        self.assertTrue(manifest["comparable"])
        self.assertEqual(len(manifest["plans"]), 1)
        self.assertEqual(len(manifest["cells"]), 4)
        instruction_paths = set()
        modes = set()
        for cell in manifest["cells"]:
            parsed = tomllib.loads(Path(cell["config"]).read_text())
            instruction_paths.add(parsed["task"]["instruction"])
            modes.add(parsed["mcp"]["state_response_mode"])
            self.assertEqual(
                parsed["mcp"]["call_contract"],
                cub.state_call_contract(parsed["mcp"]["state_response_mode"]),
            )
            loaded = load_config(cell["config"])
            self.assertEqual(loaded.state_response_mode, parsed["mcp"]["state_response_mode"])
            self.assertEqual(
                list(loaded.mcp_call_contract),
                cub.state_call_contract(loaded.state_response_mode),
            )
            self.assertEqual(
                loaded.matrix["config_identity"]["mcp"]["state_response_mode"],
                loaded.state_response_mode,
            )
        self.assertEqual(modes, {"auto", "full"})
        self.assertEqual(len(instruction_paths), 1)

    def test_post_action_state_ab_materializes_locked_distinct_arms(self):
        state_contract = cub.post_action_state_call_contract("state")
        no_state_contract = cub.post_action_state_call_contract("no-state")
        self.assertEqual(state_contract[0], no_state_contract[0])
        self.assertTrue(all(
            item["required_arguments"]["include_state"] is True
            for item in state_contract[1:]
        ))
        self.assertTrue(all(
            item["required_arguments"]["include_state"] is False
            for item in no_state_contract[1:]
        ))
        self.assertTrue(all(
            item["required_arguments"]["include_screenshot"] is False
            for item in (*state_contract, *no_state_contract)
        ))
        for invalid in ("auto", "full", "", "STATE", None):
            with self.subTest(invalid=invalid), self.assertRaises(cub.CubError):
                cub.post_action_state_call_contract(invalid)
            with self.subTest(invalid_mode=invalid), self.assertRaises(cub.CubError):
                cub.post_action_state_response_mode(invalid)
        self.assertEqual(cub.post_action_state_response_mode("state"), "auto")
        self.assertIsNone(cub.post_action_state_response_mode("no-state"))

        host = {
            "os_version": "15.6", "os_build": "24G84",
            "architecture": "arm64", "hardware": "MacFixture1,1",
            "display_width": 1512, "display_height": 982,
            "display_scale": 2.0, "display_color_space": "Color LCD",
        }
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": False, "state_ab_ready": True, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            mock.patch.object(
                cub, "_content_bound_command_digest", side_effect=self.content_digest
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cub.generate(
                    self.request, "post-action-state-ab", None, None,
                    repetitions=2,
                ),
                0,
            )

        config_root = self.run_root / "configs/post-action-state-ab"
        manifest = json.loads((config_root / "manifest.json").read_text())
        plan = json.loads(
            (config_root / "plans/post-action-state-ab.plan.json").read_text()
        )
        self.assertTrue(manifest["comparable"])
        self.assertEqual({cell["arm_id"] for cell in manifest["cells"]}, {
            "state", "no-state",
        })
        self.assertEqual([arm["id"] for arm in plan["arms"]], [
            "state", "no-state",
        ])
        instruction_paths = set()
        for cell in manifest["cells"]:
            parsed = tomllib.loads(Path(cell["config"]).read_text())
            instruction_paths.add(parsed["task"]["instruction"])
            expected_mode = cub.post_action_state_response_mode(cell["arm_id"])
            self.assertEqual(parsed["mcp"].get("state_response_mode"), expected_mode)
            self.assertEqual(
                "state_response_mode" in parsed["mcp"],
                expected_mode is not None,
            )
            self.assertEqual(
                parsed["mcp"]["call_contract"],
                cub.post_action_state_call_contract(cell["arm_id"]),
            )
            loaded = load_config(cell["config"])
            self.assertEqual(loaded.state_response_mode, expected_mode)
            self.assertEqual(
                list(loaded.mcp_call_contract),
                cub.post_action_state_call_contract(cell["arm_id"]),
            )
            planned = next(
                arm for arm in plan["arms"] if arm["id"] == cell["arm_id"]
            )
            self.assertEqual(
                planned["config_identity"]["mcp"]["call_contract"],
                parsed["mcp"]["call_contract"],
            )
            self.assertEqual(
                planned["config_identity"]["mcp"].get("state_response_mode"),
                expected_mode,
            )
            self.assertEqual(
                "state_response_mode" in planned["config_identity"]["mcp"],
                expected_mode is not None,
            )
        self.assertEqual(len(instruction_paths), 1)

    def test_post_action_state_ab_rejects_external_arm_or_task(self):
        with mock.patch.object(cub, "_static_preflight", return_value={
            "matched_ready": False, "state_ab_ready": True, "checks": []
        }):
            for arm, task in (("state", None), (None, "basic-controls")):
                with self.subTest(arm=arm, task=task), self.assertRaisesRegex(
                    cub.CubError, "fixed state/no-state arms and task"
                ):
                    cub.generate(
                        self.request, "post-action-state-ab", arm, task,
                        repetitions=1,
                    )

    def test_experiment_tasks_are_not_generic_pilots(self):
        with mock.patch.object(cub, "_static_preflight", return_value={
            "matched_ready": True, "state_ab_ready": True, "checks": []
        }):
            for task in cub.EXPERIMENT_TASKS:
                with self.subTest(task=task), self.assertRaisesRegex(
                    cub.CubError, "dedicated A/B mode"
                ):
                    cub.generate(
                        self.request, "pilot", "source", task,
                        trial_index=1,
                    )

    def test_wrap_app_refuses_stale_executable_with_matching_bundle_metadata(self):
        binary = self.base / "fixture"
        binary.write_bytes(b"new")
        app = self.base / "Fixture.app"
        app.mkdir()
        with mock.patch.object(cub, "_bundle_info", return_value={
            "bundle_id": "org.openbench.fixture",
            "version": "0.0.1",
            "binary_sha256": cub.hashlib.sha256(b"old").hexdigest(),
        }):
            with self.assertRaisesRegex(cub.CubError, "exact built executable"):
                cub._wrap_app(
                    binary,
                    app,
                    "org.openbench.fixture",
                    "0.0.1",
                    "-",
                )

    def test_public_preflight_scrubs_home_paths_and_signing_identity(self):
        full = {
            "schema_version": cub.PREFLIGHT_SCHEMA,
            "read_only": True,
            "matched_ready": False,
            "state_ab_ready": True,
            "checks": [
                {
                    "name": "run_root_safe", "passed": True,
                    "observed": "/Users/example/.openbench-runs/cub",
                    "required": "safe",
                },
                {
                    "name": "source_mcp_identity", "passed": True,
                    "observed": {
                        "app": "/Users/example/private/Source.app",
                        "executable": "/Users/example/private/Source.app/Contents/MacOS/server",
                        "bundle_id": cub.SOURCE_MCP_BUNDLE_ID,
                        "version": cub.MCP_VERSION,
                        "build": "1",
                        "binary_sha256": "a" * 64,
                        "build_stamp_unix": 123,
                        "signature_sha256": "b" * 64,
                        "designated_requirement": "certificate leaf = Alice Example",
                        "adhoc": False,
                    },
                    "required": {"version": cub.MCP_VERSION},
                },
                {
                    "name": "codex_auth", "passed": True,
                    "observed": "/Users/example/.codex/auth.json",
                    "required": "existing auth.json",
                },
            ],
            "next": {},
        }
        rendered = json.dumps(cub._publication_safe_preflight(full), sort_keys=True)
        self.assertNotIn("/Users/", rendered)
        self.assertNotIn("Alice Example", rendered)
        self.assertIn("$OPENBENCH_CUB_RUN_ROOT", rendered)
        self.assertIn('"publication_safe": true', rendered)
        self.assertIn('"state_ab_ready": true', rendered)

    def test_checked_in_request_uses_only_portable_environment_placeholders(self):
        sample = ROOT / "computer-use-tasks/v0/config-request.sample.toml"
        text = sample.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)
        self.assertNotIn("Apple Development:", text)
        with mock.patch.dict(os.environ, {
            "OPENBENCH_CUB_RUN_ROOT": str(self.run_root),
            "OPENBENCH_CUB_MCP_REPO": str(self.repo),
            "OPENBENCH_CUB_INSTALLED_MCP_APP": str(self.installed),
            "OPENBENCH_CUB_SIGNING_IDENTITY": "Developer ID Application: Fixture",
        }, clear=False):
            request = cub._load_request(sample)
        self.assertEqual(request["run_root"], str(self.run_root))
        self.assertEqual(
            request["source_signing_identity"],
            "Developer ID Application: Fixture",
        )

    def test_matched_configs_parse_lock_native_profile_and_make_no_harbor_claim(self):
        host = {
            "os_version": "15.6", "os_build": "24G84",
            "architecture": "arm64", "hardware": "MacFixture1,1",
            "display_width": 1512, "display_height": 982,
            "display_scale": 2.0, "display_color_space": "Color LCD",
        }
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": True, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            mock.patch.object(
                cub,
                "_content_bound_command_digest",
                side_effect=self.content_digest,
            ),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cub.generate(self.request, "matched", None, None), 0)

        manifest = json.loads(
            (self.run_root / "configs/matched/manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(manifest["comparable"])
        self.assertEqual(manifest["repetitions"], 5)
        self.assertEqual(len(manifest["plans"]), 3)
        self.assertEqual(len(manifest["cells"]), 30)
        workspaces = set()
        evidence_paths = set()
        process_states = set()
        output_paths = set()
        results_paths = set()
        matrix_cell_keys = set()
        setup_commands = set()
        expected_delivery_tiers = {
            "background-control": [
                "tier1-ax-action",
                "tier1-ax-attribute",
                "tier2-per-window-nsevent",
            ],
            "basic-controls": [
                "tier1-ax-action",
                "tier1-ax-attribute",
                "tier2-per-window-nsevent",
                "tier25-skylight-sleventpostto-pid",
                "tier3-cgeventpostto-pid",
                "pasteboard",
                "launchservices",
                "ax-window-management",
            ],
            "textedit-exact-file": [
                "tier1-ax-action",
                "tier1-ax-attribute",
                "tier2-per-window-nsevent",
                "tier25-skylight-sleventpostto-pid",
                "tier3-cgeventpostto-pid",
                "pasteboard",
                "launchservices",
                "ax-window-management",
            ],
        }
        plans_by_task = {}
        for plan_entry in manifest["plans"]:
            spec_path = Path(plan_entry["spec"])
            plan_path = Path(plan_entry["plan"])
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plans_by_task[plan_entry["task"]] = plan
            self.assertEqual(plan, build_native_matrix(**spec))
            self.assertEqual(validate_native_matrix(plan), plan)
            self.assertEqual(
                plan_entry["plan_command"],
                ["obench", "native", "plan", str(spec_path), "--output", str(plan_path)],
            )
            self.assertEqual(
                plan_entry["spec_sha256"],
                cub._bytes_sha256(canonical_bytes(spec) + b"\n"),
            )
            self.assertEqual(
                plan_entry["plan_file_sha256"],
                cub._bytes_sha256(canonical_bytes(plan) + b"\n"),
            )
            orders = [
                [
                    cell["arm_id"]
                    for cell in plan["schedule"]
                    if cell["block"] == block
                ]
                for block in range(1, 6)
            ]
            self.assertEqual(
                orders,
                [
                    ["installed", "source"],
                    ["source", "installed"],
                    ["source", "installed"],
                    ["installed", "source"],
                    ["installed", "source"],
                ],
            )

        for cell in manifest["cells"]:
            path = Path(cell["config"])
            with self.subTest(config=path):
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                self.assertNotIn("harbor", text.lower())
                self.assertEqual(
                    cell["runnable_config_sha256"],
                    cub._bytes_sha256(raw),
                )
                parsed = tomllib.loads(text)
                self.assertEqual(parsed["trial_id"], cell["trial_id"])
                self.assertEqual(
                    parsed["matrix"],
                    {
                        "manifest": str(
                            self.run_root / "configs/matched/manifest.json"
                        ),
                        "plan": str(
                            self.run_root
                            / f"configs/matched/plans/{cell['task']}.plan.json"
                        ),
                        "plan_sha256": cell["plan_sha256"],
                        "cell_id": cell["cell_id"],
                        "cell_sha256": cell["cell_sha256"],
                        "config_sha256": cell["config_sha256"],
                    },
                )
                self.assertEqual(
                    parsed["trial_id"],
                    f"cub-v0-{cell['task']}-{cell['arm_id']}-trial{cell['trial_index']}",
                )
                self.assertEqual(parsed["harness"]["name"], "codex")
                self.assertEqual(parsed["model"], {
                    "name": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "revision": "gpt-5.6-sol",
                })
                self.assertEqual(parsed["mcp"]["client_command_env"], "CUB_MCP_COMMAND")
                self.assertEqual(parsed["mcp"]["command"][-1], "serve")
                planned_arm = next(
                    arm
                    for arm in plans_by_task[cell["task"]]["arms"]
                    if arm["id"] == cell["arm_id"]
                )
                self.assertEqual(
                    planned_arm["config_identity"]["mcp"]["server_sha256"],
                    self.content_digest(
                        parsed["mcp"]["command"],
                        cwd=path.parent,
                    ),
                )
                self.assertTrue(parsed["proxy"]["required"])
                self.assertEqual(
                    parsed["focus"]["allowed_delivery_tiers"],
                    expected_delivery_tiers[cell["task"]],
                )
                self.assertTrue(parsed["atif_path"].endswith("trajectory.json"))
                expected_artifact_source = (
                    "artifacts/openbench-exact.txt"
                    if cell["task"] == "textedit-exact-file"
                    else "artifacts/fixture-state.json"
                )
                self.assertEqual(
                    parsed["artifacts"][0]["source"],
                    expected_artifact_source,
                )
                command = parsed["phases"]["setup"]["command"]
                self.assertEqual(command[-1], "setup")
                setup_commands.add(tuple(command))
                loaded = load_config(path)
                self.assertEqual(loaded.trial_id, cell["trial_id"])
                self.assertEqual(
                    loaded.mcp_command,
                    tuple(parsed["mcp"]["command"]),
                )
                self.assertEqual(
                    loaded.matrix["runnable_config_sha256"],
                    cell["runnable_config_sha256"],
                )
                self.assertEqual(loaded.model_name, "gpt-5.6-sol")
                self.assertTrue(loaded.proxy_required)
                self.assertEqual(
                    loaded.focus_policy["allowed_delivery_tiers"],
                    expected_delivery_tiers[cell["task"]],
                )
                self.assertEqual(loaded.mcp_client_command_env, "CUB_MCP_COMMAND")
                workspaces.add(str(loaded.workspace))
                output_paths.add(str(loaded.output_dir))
                results_paths.add(str(loaded.results_path))
                evidence_paths.add(cell["evidence"])
                process_states.add(cell["process_state"])
                matrix_cell_keys.add(cell["matrix_cell_key"])
                self.assertEqual(str(loaded.output_dir), cell["output"])
                self.assertEqual(str(loaded.results_path), cell["results"])
        for paths in (
            workspaces,
            evidence_paths,
            process_states,
            output_paths,
            results_paths,
            matrix_cell_keys,
        ):
            self.assertEqual(len(paths), 30)
        self.assertEqual(len(setup_commands), 1)

        before = {
            path: path.read_bytes()
            for path in (self.run_root / "configs/matched").rglob("*")
            if path.is_file()
        }
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": True, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            mock.patch.object(
                cub,
                "_content_bound_command_digest",
                side_effect=self.content_digest,
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cub.generate(self.request, "matched", None, None), 0)
        self.assertEqual(
            before,
            {
                path: path.read_bytes()
                for path in (self.run_root / "configs/matched").rglob("*")
                if path.is_file()
            },
        )
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": True, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            mock.patch.object(
                cub,
                "_content_bound_command_digest",
                side_effect=self.content_digest,
            ),
            self.assertRaisesRegex(cub.CubError, "divergent immutable"),
        ):
            cub.generate(
                self.request, "matched", None, None, repetitions=6
            )
        self.assertFalse(
            any(
                path.name.startswith("trial6-")
                for path in (self.run_root / "configs/matched").rglob("*.toml")
            )
        )

        bound_config = Path(manifest["cells"][0]["config"])
        bound_bytes = bound_config.read_bytes()
        bound_config.write_bytes(bound_bytes + b"\n")
        with self.assertRaisesRegex(NativeRunError, "runnable config digest"):
            load_config(bound_config)
        bound_config.write_bytes(bound_bytes)

        first_plan = json.loads(
            Path(manifest["plans"][0]["plan"]).read_text(encoding="utf-8")
        )
        first_cell = first_plan["schedule"][0]
        observation = {
            key: first_cell[key]
            for key in ("cell_id", "trial_id", "config_sha256", "cell_sha256")
        }
        observation.update(
            {"result_sha256": "a" * 64, "bundle_sha256": "b" * 64}
        )
        state = reconcile_native_state(first_plan, [observation])
        self.assertEqual(
            reconcile_native_state(first_plan, [observation], prior_state=state),
            state,
        )
        with self.assertRaisesRegex(NativeMatrixError, "different immutable"):
            reconcile_native_state(
                first_plan,
                [{**observation, "result_sha256": "c" * 64}],
                prior_state=state,
            )

        for task in cub.TASKS:
            sidecar = tomllib.loads(
                (ROOT / f"computer-use-tasks/v0/{task}/native-macos.toml").read_text(encoding="utf-8")
            )
            self.assertFalse(sidecar["harbor_execution_supported"])

    def test_pilot_config_binds_requested_trial_index(self):
        host = {
            "os_version": "15.6", "os_build": "24G84",
            "architecture": "arm64", "hardware": "MacFixture1,1",
            "display_width": 1512, "display_height": 982,
            "display_scale": 2.0, "display_color_space": "Color LCD",
        }
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": False, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cub.generate(
                    self.request,
                    "pilot",
                    "source",
                    "basic-controls",
                    trial_index=3,
                ),
                0,
            )
        manifest = json.loads(
            (self.run_root / "configs/pilot/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(manifest["cells"]), 1)
        cell = manifest["cells"][0]
        self.assertEqual(cell["trial_index"], 3)
        self.assertEqual(
            cell["trial_id"],
            "cub-v0-pilot-source-basic-controls-trial3",
        )
        config = load_config(cell["config"])
        self.assertEqual(config.trial_id, cell["trial_id"])
        for path in (
            config.workspace,
            config.output_dir,
            config.results_path,
            Path(cell["evidence"]),
            Path(cell["process_state"]),
        ):
            self.assertIn("trial3", str(path))

    def test_pilot_can_generate_both_comparison_arms_atomically(self):
        host = {
            "os_version": "15.6", "os_build": "24G84",
            "architecture": "arm64", "hardware": "MacFixture1,1",
            "display_width": 1512, "display_height": 982,
            "display_scale": 2.0, "display_color_space": "Color LCD",
        }
        with (
            mock.patch.object(cub, "_bundle_info", side_effect=self.identity),
            mock.patch.object(cub, "_static_preflight", return_value={
                "matched_ready": False, "checks": []
            }),
            mock.patch.object(cub, "_host_environment", return_value=host),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cub.generate(
                    self.request,
                    "pilot",
                    "both",
                    "textedit-exact-file",
                ),
                0,
            )

        manifest = json.loads(
            (self.run_root / "configs/pilot/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [cell["arm_id"] for cell in manifest["cells"]],
            ["installed", "source"],
        )
        self.assertEqual(
            {
                Path(cell["config"]).name
                for cell in manifest["cells"]
            },
            {"trial1-installed.toml", "trial1-source.toml"},
        )

    def test_runtime_coordinates_accept_exact_native_env_only(self):
        with mock.patch.dict(
            os.environ,
            {
                "OPENBENCH_NATIVE_TRIAL_ID":
                    "cub-v0-basic-controls-installed-trial4",
                "OPENBENCH_NATIVE_TASK_ID":
                    "openbench/computer-use-v0-basic-controls",
                "OPENBENCH_NATIVE_MCP_COLLECTOR_RUN_ID":
                    "cub-v0-installed-mcp",
            },
            clear=False,
        ):
            self.assertEqual(
                cub._runtime_coordinates(None, None, None),
                ("installed", "basic-controls", 4),
            )
        with self.assertRaisesRegex(cub.CubError, "supplied together"):
            cub._runtime_coordinates("installed", None, 1)

    def test_matched_generation_fails_closed_without_tcc_identity_proof(self):
        with mock.patch.object(cub, "_static_preflight", return_value={
            "matched_ready": False,
            "checks": [{"name": "source_tcc_identity_proof", "passed": False}],
        }):
            with self.assertRaisesRegex(cub.CubError, "identity/TCC proof"):
                cub.generate(self.request, "matched", None, None)
        self.assertFalse((self.run_root / "configs/matched").exists())


if __name__ == "__main__":
    unittest.main()
