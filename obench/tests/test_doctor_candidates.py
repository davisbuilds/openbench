#!/usr/bin/env python3
"""Doctor --candidate preflight and adapter DOCTOR discovery."""

import os
import stat
import tempfile
import textwrap
import types
import unittest

from obench import doctor
from obench.candidates import load_candidate


class FakeProbes:
    """Canned probes (mirrors test_doctor.FakeProbes; kept local for discover)."""

    def __init__(self, which_map=None, run_map=None, exists_set=None,
                 json_map=None, models_map=None, env_map=None, text_map=None,
                 http_map=None):
        self.which_map = which_map or {}
        self.run_map = run_map or {}
        self.exists_set = set(exists_set or [])
        self.json_map = json_map or {}
        self.models_map = models_map or {}
        self.env_map = env_map or {}
        self.text_map = text_map or {}
        self.http_map = http_map or {}

    def which(self, cli):
        return self.which_map.get(cli)

    def run(self, argv, timeout=15):
        return self.run_map.get(tuple(argv), (None, ""))

    def exists(self, path):
        return os.path.expanduser(path) in self.exists_set

    def read_json(self, path):
        return self.json_map.get(os.path.expanduser(path))

    def read_text(self, path):
        return self.text_map.get(os.path.expanduser(path))

    def getenv(self, name):
        return self.env_map.get(name)

    def http_get(self, url, headers=None, timeout=2.0):
        return self.http_map.get(url, (None, ""))

    def import_adapter(self, name):
        models = self.models_map.get(name)
        if models is None:
            raise FileNotFoundError(f"no adapter {name}")
        mod = types.ModuleType(f"fake_{name}")
        if isinstance(models, tuple):
            mod.MODELS, mod.OPEN_MODELS = models
        else:
            mod.MODELS = models
        return mod


def all_green_probes():
    home = os.path.expanduser("~")
    return FakeProbes(
        which_map={"codex": "/b/codex", "pi": "/b/pi", "opencode": "/b/opencode",
                   "cursor-agent": "/b/cursor-agent", "claude": "/b/claude",
                   "grok": "/b/grok", "devin": "/b/devin", "docker": "/b/docker"},
        run_map={
            ("codex", "--version"): (0, "codex-cli 0.144.5"),
            ("pi", "--version"): (0, "0.80.10"),
            ("opencode", "--version"): (0, "1.18.3"),
            ("cursor-agent", "--version"): (0, "2026.07.09-a3815c0"),
            ("docker", "info", "--format", "{{.ServerVersion}}"): (0, "27.0"),
            ("opencode", "auth", "list"): (0, "OpenAI oauth\n"),
            ("cursor-agent", "status"): (0, "Logged in as x\n"),
        },
        exists_set={os.path.join(home, ".codex", "auth.json"),
                    os.path.join(home, ".pi", "agent", "auth.json"),
                    os.path.join(home, ".config", "devin")},
        json_map={os.path.join(home, ".pi", "agent", "auth.json"):
                  {"openai-codex": {}, "anthropic": {}}},
        models_map={h: {"gpt-5.5-medium": "x"} for h in doctor.ALL_HARNESSES},
    )


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestAdapterDoctorDiscovery(unittest.TestCase):
    def test_pi_exports_doctor_and_is_discovered(self):
        discovered = doctor.discover_adapter_doctor(doctor.ADAPTERS_DIR, "pi")
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered["cli"], "pi")
        self.assertTrue(callable(discovered["auth"]))
        harnesses = doctor.load_harnesses(doctor.ADAPTERS_DIR)
        self.assertEqual(harnesses["pi"]["cli"], "pi")
        # Re-import yields a fresh function object; behavior must match.
        ok, detail = harnesses["pi"]["auth"](FakeProbes(
            exists_set={os.path.expanduser("~/.pi/agent/auth.json")},
            json_map={os.path.expanduser("~/.pi/agent/auth.json"):
                      {"openai-codex": {}}},
        ))
        self.assertTrue(ok)
        self.assertIn("openai-codex", detail)

    def test_discovery_falls_back_when_adapter_omits_doctor(self):
        with tempfile.TemporaryDirectory() as td:
            _write(os.path.join(td, "codex.py"), "NAME = 'codex'\nMODELS = {}\n")
            harnesses = doctor.load_harnesses(td)
            self.assertEqual(harnesses["codex"]["cli"], "codex")
            self.assertTrue(callable(harnesses["codex"]["auth"]))

    def test_discovery_accepts_new_adapter_doctor_export(self):
        with tempfile.TemporaryDirectory() as td:
            _write(os.path.join(td, "acme.py"), textwrap.dedent("""\
                NAME = "acme"
                MODELS = {"gpt-5.5-medium": "x"}
                def _auth(p):
                    return True, "ok"
                DOCTOR = {"cli": "acme-cli", "auth": _auth}
            """))
            harnesses = doctor.load_harnesses(td)
            self.assertEqual(harnesses["acme"]["cli"], "acme-cli")
            ok, detail = harnesses["acme"]["auth"](FakeProbes())
            self.assertTrue(ok)
            self.assertEqual(detail, "ok")


class TestManifestCandidateDoctor(unittest.TestCase):
    def _manifest(self, td, *, auth=False, pass_env=False, models=True):
        bin_dir = os.path.join(td, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        cli = os.path.join(bin_dir, "fake-cli")
        _write(cli, "#!/bin/sh\necho fake-cli 0.1.0\n")
        os.chmod(cli, os.stat(cli).st_mode | stat.S_IEXEC)
        lines = [
            'kind = "manifest"',
            'name = "fake-cli"',
            'command = ["fake-cli", "{prompt}"]',
            'version_command = ["fake-cli", "--version"]',
            'policy_headless_args = []',
            'policy_auto_approve_args = []',
        ]
        if pass_env:
            lines.append('pass_env = ["VENDOR_API_KEY"]')
        if auth:
            auth_path = os.path.join(td, "home", ".fake", "auth.json")
            _write(auth_path, '{"token":"x"}')
            lines.append("[[auth_files]]")
            lines.append('source = "~/.fake/auth.json"')
            lines.append('destination = ".fake/auth.json"')
        if models:
            lines.append("[models]")
            lines.append('"gpt-5.5-medium" = "fake-model"')
        spec = os.path.join(td, "candidate.toml")
        _write(spec, "\n".join(lines) + "\n")
        return spec, bin_dir

    def test_manifest_candidate_passes_with_cli_on_path(self):
        with tempfile.TemporaryDirectory() as td:
            spec, bin_dir = self._manifest(td)
            candidate = load_candidate(spec, doctor.ADAPTERS_DIR)
            probes = FakeProbes(
                which_map={"fake-cli": os.path.join(bin_dir, "fake-cli")},
                run_map={("fake-cli", "--version"): (0, "fake-cli 0.1.0\n")},
            )
            rows, ok = doctor.evaluate(
                [candidate.name], "gpt-5.5-medium", probes,
                candidates={candidate.name: candidate})
            self.assertTrue(ok)
            by_check = {r["check"]: r for r in rows}
            self.assertTrue(by_check["CLI"]["ok"])
            self.assertTrue(by_check["VERSION"]["ok"])
            self.assertTrue(by_check["AUTH"]["ok"])
            self.assertTrue(by_check["MODEL"]["ok"])

    def test_manifest_missing_binary_fails(self):
        with tempfile.TemporaryDirectory() as td:
            spec, _bin = self._manifest(td)
            candidate = load_candidate(spec, doctor.ADAPTERS_DIR)
            probes = FakeProbes(which_map={})
            rows, ok = doctor.evaluate(
                [candidate.name], "gpt-5.5-medium", probes,
                candidates={candidate.name: candidate})
            self.assertFalse(ok)
            cli = next(r for r in rows if r["check"] == "CLI")
            self.assertFalse(cli["ok"])
            self.assertIn("not found", cli["detail"])

    def test_manifest_missing_auth_fails(self):
        with tempfile.TemporaryDirectory() as td:
            spec, bin_dir = self._manifest(td, auth=True)
            candidate = load_candidate(spec, doctor.ADAPTERS_DIR)
            probes = FakeProbes(
                which_map={"fake-cli": os.path.join(bin_dir, "fake-cli")},
                run_map={("fake-cli", "--version"): (0, "fake-cli 0.1.0\n")},
                exists_set=[],
            )
            rows, ok = doctor.evaluate(
                [candidate.name], "gpt-5.5-medium", probes,
                candidates={candidate.name: candidate})
            self.assertFalse(ok)
            auth = next(r for r in rows if r["check"] == "AUTH")
            self.assertFalse(auth["ok"])
            self.assertIn("missing", auth["detail"])

    def test_manifest_missing_pass_env_warns_not_fails(self):
        with tempfile.TemporaryDirectory() as td:
            spec, bin_dir = self._manifest(td, pass_env=True)
            candidate = load_candidate(spec, doctor.ADAPTERS_DIR)
            probes = FakeProbes(
                which_map={"fake-cli": os.path.join(bin_dir, "fake-cli")},
                run_map={("fake-cli", "--version"): (0, "fake-cli 0.1.0\n")},
            )
            rows, ok = doctor.evaluate(
                [candidate.name], "gpt-5.5-medium", probes,
                candidates={candidate.name: candidate})
            self.assertTrue(ok)
            env = next(r for r in rows if r["check"] == "ENV")
            self.assertIsNone(env["ok"])
            self.assertIn("WARN unset", env["detail"])

    def test_unknown_harness_suggests_candidate(self):
        rows, ok = doctor.evaluate(["nope"], "gpt-5.5-medium", all_green_probes())
        self.assertFalse(ok)
        self.assertIn("unknown harness", rows[0]["detail"])
        self.assertIn("--candidate", rows[0]["detail"])


class TestConfigVariantDoctor(unittest.TestCase):
    def test_config_variant_checks_base_plus_config_files(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = os.path.join(td, "cfg")
            _write(os.path.join(cfg_dir, "config.toml"), "x = 1\n")
            spec = os.path.join(td, "variant.toml")
            _write(spec, textwrap.dedent(f"""\
                kind = "config-variant"
                name = "codex-cfg"
                base_adapter = "codex"
                config_dir = "{cfg_dir}"
                config_files = ["config.toml"]
            """))
            candidate = load_candidate(spec, doctor.ADAPTERS_DIR)
            probes = all_green_probes()
            probes.exists_set.add(cfg_dir)
            probes.exists_set.add(os.path.join(cfg_dir, "config.toml"))
            rows, ok = doctor.evaluate(
                [candidate.name], "gpt-5.5-medium", probes,
                candidates={candidate.name: candidate})
            self.assertTrue(ok)
            config = next(r for r in rows if r["check"] == "CONFIG")
            self.assertTrue(config["ok"])

    def test_config_variant_missing_config_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = os.path.join(td, "cfg")
            _write(os.path.join(cfg_dir, "config.toml"), "x = 1\n")
            spec = os.path.join(td, "variant.toml")
            _write(spec, textwrap.dedent(f"""\
                kind = "config-variant"
                name = "codex-cfg"
                base_adapter = "codex"
                config_dir = "{cfg_dir}"
                config_files = ["config.toml"]
            """))
            candidate = load_candidate(spec, doctor.ADAPTERS_DIR)
            probes = all_green_probes()
            probes.exists_set.add(cfg_dir)
            rows, ok = doctor.evaluate(
                [candidate.name], "gpt-5.5-medium", probes,
                candidates={candidate.name: candidate})
            self.assertFalse(ok)
            config = next(r for r in rows if r["check"] == "CONFIG")
            self.assertFalse(config["ok"])


if __name__ == "__main__":
    unittest.main()
