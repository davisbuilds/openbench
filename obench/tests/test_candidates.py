#!/usr/bin/env python3
import importlib.util
import json
import os

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import shutil
import tempfile
import unittest
from unittest import mock

ADAPTERS = os.path.join(BENCH, "adapters")

import sys
from obench import candidates
from obench import run as bench_run


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ADAPTERS, name + ".py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def fixed_temporary_directory(path):
    class FixedTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self.name = path
            os.makedirs(path, exist_ok=True)

        def __enter__(self):
            return self.name

        def __exit__(self, *args):
            self.cleanup()

        def cleanup(self):
            shutil.rmtree(self.name, ignore_errors=True)

    return FixedTemporaryDirectory


class CandidateTests(unittest.TestCase):
    def test_config_variant_rejects_source_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = os.path.join(td, "config")
            os.makedirs(config_dir)
            outside = os.path.join(td, "outside")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("private")
            os.symlink(outside, os.path.join(config_dir, "linked.toml"))
            spec_path = os.path.join(td, "candidate.toml")
            with open(spec_path, "w", encoding="utf-8") as fh:
                fh.write('kind="config-variant"\nname="bad"\nbase_adapter="pi"\n'
                         'config_dir="config"\nconfig_files=["linked.toml"]\n')
            with self.assertRaisesRegex(ValueError, "escapes config_dir"):
                candidates.load_candidate(spec_path, ADAPTERS)

    def test_checked_in_codex_multiagent_candidate_loads_explicit_opt_in(self):
        path = os.path.join(
            os.path.dirname(BENCH), "experiments", "multiagent-toggle", "codex-on.toml")
        variant = candidates.load_candidate(path, ADAPTERS)
        self.assertEqual(variant.name, "codex-multiagent-on")
        self.assertEqual(variant.base_adapter, "codex")
        self.assertEqual(variant.env["OPENBENCH_CODEX_MULTI_AGENT"], "enabled")
        self.assertEqual(variant.env["CODEX_HOME"], "{config_dir}")

    def test_v2_variant_matches_ad_hoc_command_and_environment(self):
        helper = load("_codex_ablation")
        spec_path = os.path.join(os.path.dirname(BENCH), "ablation", "codex-home-v2", "candidate.toml")
        with tempfile.TemporaryDirectory() as td:
            auth = os.path.join(td, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                fh.write("{}")
            variant = candidates.load_candidate(spec_path, ADAPTERS)
            variant.auth_files[0]["source"] = auth
            captures = []
            configs = []
            def run(cmd, **kw):
                captures.append((cmd, kw["env"]))
                config_home = kw["env"]["CODEX_HOME"]
                with open(os.path.join(config_home, "config.toml"), encoding="utf-8") as fh:
                    configs.append(fh.read().replace(config_home, "<CONFIG_DIR>"))
                return Proc()
            fixed_home = os.path.join(td, "fixed-codex-home")
            with mock.patch.object(helper, "_source_codex_home", return_value=td), \
                 mock.patch.object(candidates, "_auth_source", return_value=auth), \
                 mock.patch("subprocess.run", side_effect=run), \
                 mock.patch.object(tempfile, "TemporaryDirectory",
                                   fixed_temporary_directory(fixed_home)):
                helper.run_variant("codex-v2", "v2", "prompt", td, "gpt-5.6-sol", 9)
                variant.run("prompt", td, "gpt-5.6-sol", 9)
            self.assertEqual(captures[0][0], captures[1][0])
            self.assertEqual(captures[0][1], captures[1][1])
            self.assertEqual(configs[0], configs[1])

    def test_pi_manifest_matches_native_argv_and_environment(self):
        pi = load("pi")
        manifest_path = os.path.join(BENCH, "examples", "pi-harness.toml")
        manifest = candidates.load_candidate(manifest_path, ADAPTERS)
        with tempfile.TemporaryDirectory() as td:
            auth = os.path.join(td, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                fh.write('{"openai-codex":{}}')
            pi._REAL_AUTH = auth
            manifest.auth_files[0]["source"] = auth
            captures = []
            def run(cmd, **kw): captures.append((cmd, kw["env"])); return Proc()
            fixed_home = os.path.join(td, "fixed-pi-home")
            with mock.patch("subprocess.run", side_effect=run), \
                 mock.patch.object(candidates, "_auth_source", return_value=auth), \
                 mock.patch.object(candidates, "_run_process", side_effect=run), \
                 mock.patch.object(pi.tempfile, "mkdtemp", return_value=fixed_home), \
                 mock.patch.object(tempfile, "TemporaryDirectory",
                                   fixed_temporary_directory(fixed_home)):
                pi.run("prompt", td, "gpt-5.5-medium", 9)
                manifest.run("prompt", td, "gpt-5.5-medium", 9)
            self.assertEqual(captures[0][0], captures[1][0])
            self.assertEqual(captures[0][1], captures[1][1])

    def test_candidate_name_must_be_portable_identifier(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad/name"\ncommand=["cli"]\n')
            with self.assertRaisesRegex(ValueError, "candidate name must match"):
                candidates.load_candidate(path, ADAPTERS)

    def test_candidate_run_id_includes_content_identity(self):
        manifest = candidates.load_candidate(
            os.path.join(BENCH, "examples", "pi-harness.toml"), ADAPTERS)
        run_id = bench_run.make_run_id(
            manifest.name, "task", "model", 1, manifest.identity_digest)
        self.assertTrue(run_id.startswith("pi-manifest@" + manifest.identity_digest[:12]))
        self.assertEqual(manifest.provenance["candidate_digest"], manifest.identity_digest)

    def test_candidate_name_cannot_replace_requested_stock_harness(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="pi"\ncommand=["cli"]\n')
            with self.assertRaises(SystemExit):
                bench_run.main([
                    "--task", "unused", "--harness", "pi", "--candidate", path,
                    "--results-path", os.path.join(td, "results.jsonl"),
                ])

    def test_manifest_preserves_unrelated_json_braces(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="json"\ncommand=["cli", "{prompt}", "{\\\"type\\\":\\\"json\\\"}"]\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = []
            def run(cmd, **kw):
                captured.extend(cmd)
                return Proc()
            with mock.patch.object(candidates, "_run_process", side_effect=run):
                manifest.run("prompt {kept}", td, "model", 9)
            self.assertEqual(captured, ["cli", "prompt {kept}", '{"type":"json"}'])

    def test_manifest_expands_workspace_file_globs_into_argv(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "src"))
            for relative in ("main.py", "src/helper.py", "notes.txt"):
                with open(os.path.join(td, relative), "w", encoding="utf-8") as fh:
                    fh.write(relative)
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="files"\n'
                         'command=["cli", "{workspace_files}", "{prompt}"]\n'
                         'workspace_file_globs=["**/*.py"]\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = []
            def run(cmd, **kw):
                captured.extend(cmd)
                return Proc()
            with mock.patch.object(candidates, "_run_process", side_effect=run):
                manifest.run("fix it", td, "model", 9)
            self.assertEqual(captured, ["cli", "main.py", "src/helper.py", "fix it"])
            self.assertEqual(manifest.provenance["workspace_file_globs"], ["**/*.py"])

    def test_manifest_rejects_workspace_file_glob_escape(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\n'
                         'command=["cli", "{workspace_files}"]\n'
                         'workspace_file_globs=["../*"]\n')
            with self.assertRaisesRegex(ValueError, "must stay within workspace"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_version_uses_isolated_home(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="versioned"\ncommand=["cli"]\n'
                         'version_command=["cli", "--home", "{home}"]\ninherit_env=true\n'
                         '[env]\nTOOL_HOME="{home}/tool"\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = {}
            def run(cmd, **kw):
                captured.update(kw)
                captured["cmd"] = cmd
                return type("VersionProc", (), {"returncode": 0, "stdout": "1.0", "stderr": ""})()
            with mock.patch.object(candidates, "_run_process", side_effect=run):
                self.assertEqual(manifest.version(), "1.0")
            self.assertNotEqual(captured["env"]["HOME"], os.path.expanduser("~"))
            self.assertEqual(captured["cwd"], captured["env"]["HOME"])
            self.assertEqual(captured["env"]["TOOL_HOME"], captured["cwd"] + "/tool")
            self.assertEqual(captured["cmd"][-1], captured["cwd"])
            self.assertFalse(os.path.exists(captured["cwd"]))

            failed = type("VersionProc", (), {
                "returncode": 2, "stdout": "", "stderr": "bad flag",
            })()
            with mock.patch.object(candidates, "_run_process", return_value=failed):
                self.assertIsNone(manifest.version())

    def test_manifest_cannot_override_isolated_home(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\n'
                         '[env]\nHOME="/tmp/not-isolated"\n')
            with self.assertRaisesRegex(ValueError, "cannot override HOME"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_does_not_inherit_host_secrets_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="safe"\ncommand=["cli"]\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = {}
            def run(cmd, **kw):
                captured.update(kw["env"])
                return Proc()
            with mock.patch.dict(os.environ, {"UNRELATED_API_SECRET": "do-not-forward"}), \
                 mock.patch.object(candidates, "_run_process", side_effect=run):
                manifest.run("prompt", td, "model", 9)
            self.assertNotIn("UNRELATED_API_SECRET", captured)

    def test_manifest_proxy_base_url_override(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    'kind="manifest"\nname="proxy-cli"\ncommand=["cli", "{prompt}"]\n'
                    'base_url_env="CLI_BASE_URL"\nproxy_route="chat/zai/api/paas/v4"\n'
                )
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = {}
            def run(cmd, **kw):
                captured.update(kw["env"])
                return Proc()
            proxy_env = {
                "OPENBENCH_PROXY": "1",
                "OPENBENCH_PROXY_BASE_URL": "http://127.0.0.1:1234",
                "OPENBENCH_PROXY_CELL_TOKEN": "cell-token",
            }
            with mock.patch.dict(os.environ, proxy_env, clear=False), \
                 mock.patch.object(candidates, "_run_process", side_effect=run):
                manifest.run("prompt", td, "model", 9)
            self.assertEqual(
                captured["CLI_BASE_URL"],
                "http://127.0.0.1:1234/cell/cell-token/chat/zai/api/paas/v4",
            )

    def test_manifest_timeout_kills_descendants(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "survived")
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="timeout"\n'
                         f'command=["/bin/sh", "-c", "(sleep 0.4; touch {marker}) & wait"]\n')
            result = candidates.load_candidate(path, ADAPTERS).run("prompt", td, "model", 0.1)
            self.assertIn("timeout", result["error"])
            time.sleep(0.5)
            self.assertFalse(os.path.exists(marker))

    def test_manifest_rejects_empty_command(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="empty"\ncommand=[]\n')
            with self.assertRaisesRegex(ValueError, "non-empty"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_model_map_rejects_unknown_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="pinned"\ncommand=["cli", "{model}"]\n'
                         '[models]\n"known"="cli-known"\n')
            result = candidates.load_candidate(path, ADAPTERS).run("prompt", td, "other", 1)
            self.assertIn("unsupported-model", result["error"])
            self.assertIsNone(result["cmd"])

    def test_auth_source_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            os.makedirs(os.path.join(home, ".cli"))
            outside = os.path.join(td, "outside")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("not auth")
            link = os.path.join(home, ".cli", "auth.json")
            os.symlink(outside, link)
            original = os.path.expanduser
            def expand(value):
                if value == "~":
                    return home
                if value.startswith("~/"):
                    return os.path.join(home, value[2:])
                return original(value)
            with mock.patch.object(candidates.os.path, "expanduser", side_effect=expand):
                with self.assertRaisesRegex(ValueError, "escapes user home"):
                    candidates._auth_source("~/.cli/auth.json")

    def test_manifest_rejects_absolute_auth_source(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\n'
                         '[[auth_files]]\nsource="/tmp/auth"\ndestination="auth"\n')
            with self.assertRaisesRegex(ValueError, "home-relative"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_rejects_runner_owned_proxy_environment(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\n'
                         '[env]\nOPENBENCH_PROXY_CELL_TOKEN="other"\n')
            with self.assertRaisesRegex(ValueError, "runner proxy variables"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_requires_complete_proxy_pair(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\nproxy_route="chat/zai/v1"\n')
            with self.assertRaisesRegex(ValueError, "base_url_env and proxy_route"):
                candidates.load_candidate(path, ADAPTERS)

    def test_candidate_proxy_capable_from_manifest_declaration(self):
        """Proxy gating is declaration-driven; no stock PROXY_HARNESSES lookup."""
        with tempfile.TemporaryDirectory() as td:
            metered = os.path.join(td, "metered.toml")
            with open(metered, "w", encoding="utf-8") as fh:
                fh.write(
                    'kind="manifest"\nname="third-party"\ncommand=["cli"]\n'
                    'base_url_env="CLI_BASE_URL"\nproxy_route="chat/vendor/v1"\n'
                )
            unmetered = os.path.join(td, "unmetered.toml")
            with open(unmetered, "w", encoding="utf-8") as fh:
                fh.write(
                    'kind="manifest"\nname="offline"\nunmetered=true\ncommand=["cli"]\n'
                )
            bare = os.path.join(td, "bare.toml")
            with open(bare, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bare"\ncommand=["cli"]\n')
            m = candidates.load_candidate(metered, ADAPTERS)
            u = candidates.load_candidate(unmetered, ADAPTERS)
            b = candidates.load_candidate(bare, ADAPTERS)
            self.assertTrue(candidates.candidate_proxy_capable(m))
            self.assertFalse(candidates.candidate_proxy_capable(u))
            self.assertFalse(candidates.candidate_proxy_capable(b))
            self.assertTrue(u.unmetered)

    def test_persist_auth_defaults_off_and_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            master = os.path.join(os.path.expanduser("~"),
                                  ".openbench-test-persist-auth.json")
            try:
                with open(master, "wb") as fh:
                    fh.write(b'{"provider":"x","refresh_token":"old"}')
                off_path = os.path.join(td, "off.toml")
                with open(off_path, "w", encoding="utf-8") as fh:
                    fh.write(
                        'kind="manifest"\nname="no-persist"\nisolate_home=true\n'
                        'command=["true", "{prompt}"]\n'
                        '[[auth_files]]\n'
                        'source="~/.openbench-test-persist-auth.json"\n'
                        'destination=".mycli/auth.json"\n'
                    )
                on_path = os.path.join(td, "on.toml")
                with open(on_path, "w", encoding="utf-8") as fh:
                    fh.write(
                        'kind="manifest"\nname="yes-persist"\nisolate_home=true\n'
                        'persist_auth=true\n'
                        'command=["true", "{prompt}"]\n'
                        '[[auth_files]]\n'
                        'source="~/.openbench-test-persist-auth.json"\n'
                        'destination=".mycli/auth.json"\n'
                    )
                off = candidates.load_candidate(off_path, ADAPTERS)
                on = candidates.load_candidate(on_path, ADAPTERS)
                self.assertFalse(off.persist_auth)
                self.assertTrue(on.persist_auth)
                self.assertEqual(candidates.candidate_auth_persist_targets(off), [])
                targets = candidates.candidate_auth_persist_targets(on)
                self.assertEqual(len(targets), 1)
                self.assertEqual(targets[0][1], ".mycli/auth.json")

                class Proc:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                def run_mutate(cmd, **kw):
                    copy = os.path.join(kw["env"]["HOME"], ".mycli", "auth.json")
                    with open(copy, "wb") as fh:
                        fh.write(b'{"provider":"x","refresh_token":"rotated"}')
                    return Proc()

                with mock.patch.object(candidates, "_run_process", side_effect=run_mutate):
                    off.run("prompt", td, "model", 5)
                with open(master, "rb") as fh:
                    self.assertEqual(fh.read(), b'{"provider":"x","refresh_token":"old"}')

                with mock.patch.object(candidates, "_run_process", side_effect=run_mutate):
                    on.run("prompt", td, "model", 5)
                with open(master, "rb") as fh:
                    self.assertEqual(fh.read(), b'{"provider":"x","refresh_token":"rotated"}')
            finally:
                try:
                    os.unlink(master)
                except FileNotFoundError:
                    pass

    def test_manifest_rejects_auth_destination_escape(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\nisolate_home=true\n'
                         'command=["cli", "{prompt}"]\n'
                         '[[auth_files]]\nsource="~/auth"\ndestination="../auth"\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            with self.assertRaises(ValueError):
                manifest.run("prompt", td, "model", 1)

    def test_provenance_omits_environment_values(self):
        manifest = candidates.load_candidate(os.path.join(BENCH, "examples", "pi-harness.toml"), ADAPTERS)
        text = json.dumps(manifest.provenance)
        self.assertIn("env_names", text)
        self.assertNotIn("{home}/.pi/agent", text)


if __name__ == "__main__": unittest.main()
