import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest import mock

from obench.native_run import load_config


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "computer-use-tasks/v0/scripts/cub_v0.py"
SPEC = importlib.util.spec_from_file_location("cub_v0", SCRIPT)
cub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cub)


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

    def test_path_safety_rejects_user_roots_and_escape(self):
        for path in ("relative", "/", str(Path.home()), str(Path.home() / "Documents")):
            with self.subTest(path=path), self.assertRaises(cub.CubError):
                cub.safe_run_root(path)
        root = cub.safe_run_root(self.run_root)
        self.assertEqual(cub.descendant(root, "workspaces/task"), root / "workspaces/task")
        with self.assertRaises(cub.CubError):
            cub.descendant(root, "../escape")

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
        workspace = cub._workspace(self.run_root, "installed", "basic-controls")
        (workspace / "artifacts").mkdir(parents=True)
        (workspace / "artifacts/state.json").write_text("{}", encoding="utf-8")
        unrelated = self.base / "user-document.txt"
        unrelated.write_text("keep", encoding="utf-8")
        cub.reset_runtime(self.request, "installed", "basic-controls")
        cub.reset_runtime(self.request, "installed", "basic-controls")
        self.assertFalse((workspace / "artifacts").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

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

    def test_public_preflight_scrubs_home_paths_and_signing_identity(self):
        full = {
            "schema_version": cub.PREFLIGHT_SCHEMA,
            "read_only": True,
            "matched_ready": False,
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
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cub.generate(self.request, "matched", None, None), 0)

        configs = sorted((self.run_root / "configs/matched").glob("*/*.toml"))
        self.assertEqual(len(configs), 6)
        for path in configs:
            with self.subTest(config=path):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("harbor", text.lower())
                parsed = tomllib.loads(text)
                self.assertEqual(parsed["harness"]["name"], "codex")
                self.assertEqual(parsed["model"], {
                    "name": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "revision": "gpt-5.6-sol",
                })
                self.assertEqual(parsed["mcp"]["client_command_env"], "CUB_MCP_COMMAND")
                self.assertTrue(parsed["proxy"]["required"])
                self.assertTrue(parsed["atif_path"].endswith("trajectory.json"))
                loaded = load_config(path)
                self.assertEqual(loaded.model_name, "gpt-5.6-sol")
                self.assertTrue(loaded.proxy_required)
                self.assertEqual(loaded.mcp_client_command_env, "CUB_MCP_COMMAND")

        manifest = json.loads(
            (self.run_root / "configs/matched/manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(manifest["comparable"])
        for task in cub.TASKS:
            sidecar = tomllib.loads(
                (ROOT / f"computer-use-tasks/v0/{task}/native-macos.toml").read_text(encoding="utf-8")
            )
            self.assertFalse(sidecar["harbor_execution_supported"])

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
