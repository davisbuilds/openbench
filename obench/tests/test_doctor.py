#!/usr/bin/env python3
"""Unit tests for doctor.py evaluation, exit-code, and rendering logic.

All probes are mocked; no live CLI/auth/network calls are made.
"""

import contextlib
import io
import json
import os
import sys
import types
import unittest


from obench import doctor  # noqa: E402


class FakeProbes:
    """Canned probe responses so evaluate() runs without touching the system.

    ``which_map``   : cli -> path|None
    ``run_map``     : tuple(argv) -> (exit_code, output)
    ``exists_set``  : set of paths (expanded) that "exist"
    ``json_map``    : path (expanded) -> parsed dict
    ``models_map``  : harness -> MODELS dict (None => import raises)
    ``env_map``     : environment variable -> value
    ``text_map``    : path (expanded) -> file text
    ``http_map``    : URL -> (status, body)
    ``toml_map``    : path (expanded) -> TOML dict (for env-requirements)
    ``listdir_map`` : path -> [entries]
    ``isdir_map``   : path -> bool
    ``isfile_map``  : path -> bool
    """

    def __init__(self, which_map=None, run_map=None, exists_set=None,
                 json_map=None, models_map=None, env_map=None, text_map=None,
                 http_map=None, toml_map=None, listdir_map=None,
                 isdir_map=None, isfile_map=None):
        self.which_map = which_map or {}
        self.run_map = run_map or {}
        self.exists_set = set(exists_set or [])
        self.json_map = json_map or {}
        self.models_map = models_map or {}
        self.env_map = env_map or {}
        self.text_map = text_map or {}
        self.http_map = http_map or {}
        self.toml_map = toml_map or {}
        self.listdir_map = listdir_map or {}
        self.isdir_map = isdir_map or {}
        self.isfile_map = isfile_map or {}

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

    def read_toml(self, path):
        return self.toml_map.get(os.path.expanduser(path))

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

    def listdir(self, path):
        return self.listdir_map.get(path, [])

    def isdir(self, path):
        return self.isdir_map.get(path, False)

    def isfile(self, path):
        return self.isfile_map.get(path, False)


def all_green_probes():
    """A FakeProbes where every check passes for all five harnesses."""
    home = os.path.expanduser("~")
    return FakeProbes(
        which_map={"codex": "/b/codex", "pi": "/b/pi", "opencode": "/b/opencode",
                   "cursor-agent": "/b/cursor-agent", "claude": "/b/claude",
                   "grok": "/b/grok", "devin": "/b/devin", "docker": "/b/docker",
                   "cliproxyapi": "/b/cliproxyapi"},
        run_map={
            ("codex", "--version"): (0, "codex-cli 0.144.5"),
            ("pi", "--version"): (0, "0.80.10"),
            ("opencode", "--version"): (0, "1.18.3"),
            ("cursor-agent", "--version"): (0, "2026.07.09-a3815c0"),
            ("claude", "--version"): (0, "2.1.214 (Claude Code)"),
            ("grok", "--version"): (0, "grok 0.2.103 (hash)"),
            ("devin", "--version"): (0, "devin 1"),
            ("docker", "info", "--format", "{{.ServerVersion}}"): (0, "27.0"),
            ("docker", "info", "--format", "{{.NCPU}}"): (0, "8"),
            ("docker", "info", "--format", "{{.MemTotal}}"): (0, "17179869184"),
            ("docker", "inspect", "--format", "{{json .Config.Labels}}",
             "openbench-harness:latest"): (0, json.dumps({
                 "org.openbench.cli.codex": "0.144.5",
                 "org.openbench.cli.pi": "0.80.10",
                 "org.openbench.cli.opencode": "1.18.3",
                 "org.openbench.cli.cursor": "2026.07.09-a3815c0",
                 "org.openbench.cli.claude": "2.1.214",
                 "org.openbench.cli.grok": "0.2.103",
             })),
            ("opencode", "auth", "list"): (0, "OpenAI oauth\n"),
            ("cursor-agent", "status"): (0, "Logged in as x\n"),
            ("docker", "buildx", "version"): (0, "buildx 0.20.0"),
        },
        exists_set={os.path.join(home, ".codex", "auth.json"),
                    os.path.join(home, ".pi", "agent", "auth.json"),
                    os.path.join(home, ".config", "devin")},
        json_map={os.path.join(home, ".pi", "agent", "auth.json"):
                  {"openai-codex": {}, "anthropic": {}}},
        models_map={h: {"gpt-5.5-medium": "x"} for h in doctor.ALL_HARNESSES},
        http_map={"http://127.0.0.1:8317/v1/models":
                  (200, '{"data":[{"id":"gpt-5.6"}]}')},
        env_map={"ANTHROPIC_API_KEY": "sk-test", "OPENAI_API_KEY": "sk-test"},
    )


class TestEvaluate(unittest.TestCase):
    def test_all_green_passes(self):
        rows, ok = doctor.evaluate(
            doctor.ALL_HARNESSES, "gpt-5.5-medium", all_green_probes())
        self.assertTrue(ok)
        # Pinned harnesses have four passing checks; unpinned Devin is INFO.
        self.assertEqual(len(rows), len(doctor.ALL_HARNESSES) * 4)
        self.assertTrue(all(r["ok"] is not False for r in rows))

    def test_grok_gpt56_requires_reachable_cliproxyapi(self):
        p = all_green_probes()
        p.models_map["grokbuild"] = ({}, {"gpt-5.6": {"model_id": "gpt-5.6"}})
        del p.which_map["cliproxyapi"]
        rows, ok = doctor.evaluate(["grokbuild"], "gpt-5.6", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertIn("brew install cliproxyapi", auth["detail"])

        p.env_map["CLIPROXYAPI_BASE_URL"] = "https://bridge.example/v1"
        p.http_map["https://bridge.example/v1/models"] = (200, '{"data":[{"id":"gpt-5.6"}]}')
        rows, ok = doctor.evaluate(["grokbuild"], "gpt-5.6", p)
        self.assertTrue(ok)  # configured remote ingress needs no local binary
        p.env_map.pop("CLIPROXYAPI_BASE_URL")

        p.which_map["cliproxyapi"] = "/b/cliproxyapi"
        rows, ok = doctor.evaluate(["grokbuild"], "gpt-5.6", p)
        self.assertTrue(ok)
        self.assertIn("route ready", next(r for r in rows if r["check"] == "AUTH")["detail"])

        p.env_map["CLIPROXYAPI_BASE_URL"] = "http://127.0.0.1:notaport/v1"
        rows, ok = doctor.evaluate(["grokbuild"], "gpt-5.6", p)
        self.assertFalse(ok)
        self.assertIn("invalid port", next(r for r in rows if r["check"] == "AUTH")["detail"])

    def test_missing_cli_fails(self):
        p = all_green_probes()
        del p.which_map["codex"]
        rows, ok = doctor.evaluate(["codex"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        cli = next(r for r in rows if r["check"] == "CLI")
        self.assertFalse(cli["ok"])
        self.assertIn("not found", cli["detail"])

    def test_version_drift_fails_and_names_host_and_pin(self):
        p = all_green_probes()
        p.run_map[("codex", "--version")] = (0, "codex-cli 0.144.0")
        rows, ok = doctor.evaluate(["codex"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        version = next(r for r in rows if r["check"] == "VERSION")
        self.assertFalse(version["ok"])
        self.assertIn("host=0.144.0 pin=0.144.5 [drift]", version["detail"])

    def test_missing_auth_fails(self):
        p = all_green_probes()
        p.exists_set.discard(os.path.expanduser("~/.codex/auth.json"))
        rows, ok = doctor.evaluate(["codex"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertFalse(auth["ok"])

    def test_pi_auth_requires_recognized_entry(self):
        p = all_green_probes()
        p.json_map[os.path.expanduser("~/.pi/agent/auth.json")] = {"other": {}}
        rows, ok = doctor.evaluate(["pi"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertFalse(auth["ok"])

    def test_opencode_auth_needs_oauth_line(self):
        p = all_green_probes()
        # Only the API-key env line present, no oauth credential.
        p.run_map[("opencode", "auth", "list")] = (0, "OpenAI OPENAI_API_KEY\n")
        rows, ok = doctor.evaluate(["opencode"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertFalse(auth["ok"])

    def test_model_pin_unresolvable_fails(self):
        p = all_green_probes()
        p.models_map["codex"] = {"some-other-model": "x"}
        rows, ok = doctor.evaluate(["codex"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        model = next(r for r in rows if r["check"] == "MODEL")
        self.assertFalse(model["ok"])
        self.assertIn("not in MODELS", model["detail"])

    def test_adapter_import_failure_fails_model(self):
        p = all_green_probes()
        p.models_map.pop("devin")  # import_adapter will raise
        rows, ok = doctor.evaluate(["devin"], "gpt-5.5-medium", p)
        self.assertFalse(ok)
        model = next(r for r in rows if r["check"] == "MODEL")
        self.assertFalse(model["ok"])
        self.assertIn("import failed", model["detail"])

    def test_unknown_harness_fails(self):
        rows, ok = doctor.evaluate(["nope"], "gpt-5.5-medium", all_green_probes())
        self.assertFalse(ok)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("unknown harness", rows[0]["detail"])

    def test_frontier_pi_requires_anthropic_entry(self):
        p = all_green_probes()
        p.models_map["pi"] = {"claude-opus-4-8": {"provider": "anthropic"}}
        p.json_map[os.path.expanduser("~/.pi/agent/auth.json")] = {"openai-codex": {}}
        rows, ok = doctor.evaluate(["pi"], "claude-opus-4-8", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertFalse(auth["ok"])
        self.assertIn("anthropic", auth["detail"])

    def test_frontier_opencode_requires_anthropic_oauth(self):
        p = all_green_probes()
        p.models_map["opencode"] = {"claude-opus-4-8": "anthropic/claude-opus-4-8"}
        p.run_map[("opencode", "auth", "list")] = (0, "OpenAI oauth\n")
        rows, ok = doctor.evaluate(["opencode"], "claude-opus-4-8", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertFalse(auth["ok"])
        self.assertIn("anthropic", auth["detail"])

    def test_frontier_codex_accepts_keys_env_not_container_secret(self):
        p = all_green_probes()
        p.env_map.pop("ANTHROPIC_API_KEY", None)
        p.models_map["codex"] = ({}, {"claude-opus-4-8": {"model_id": "claude-opus-4-8"}})
        p.text_map[os.path.expanduser("~/.openbench/keys.env")] = "ANTHROPIC_API_KEY=sk-test\n"
        rows, ok = doctor.evaluate(["codex"], "claude-opus-4-8", p)
        self.assertTrue(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertTrue(auth["ok"])
        self.assertIn("keys.env", auth["detail"])

    def test_frontier_cursor_uses_local_login_outside_container(self):
        p = all_green_probes()
        p.models_map["cursor"] = {"claude-opus-4-8": "claude-opus-4-8-thinking-medium"}
        rows, ok = doctor.evaluate(["cursor"], "claude-opus-4-8", p)
        self.assertTrue(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertIn("Logged in", auth["detail"])

    def test_frontier_cursor_accepts_container_auth_dir_in_container(self):
        p = all_green_probes()
        p.models_map["cursor"] = {"claude-opus-4-8": "claude-opus-4-8-thinking-medium"}
        p.env_map["BENCH_IN_CONTAINER"] = "1"
        p.run_map[("cursor-agent", "status")] = (1, "not logged in")
        p.exists_set.add(os.path.expanduser("~/.openbench/cursor-container-auth/.config/cursor/auth.json"))
        rows, ok = doctor.evaluate(["cursor"], "claude-opus-4-8", p)
        self.assertTrue(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertIn("cursor-container-auth", auth["detail"])

    def test_grokbuild_open_model_uses_grok_cli_and_vendor_key(self):
        p = all_green_probes()
        p.models_map["grokbuild"] = ({}, {"deepseek-v4-flash": {"model_id": "deepseek-v4-flash"}})
        p.env_map["DEEPSEEK_API_KEY"] = "sk-test"
        rows, ok = doctor.evaluate(["grokbuild"], "deepseek-v4-flash", p)
        self.assertTrue(ok)
        cli = next(r for r in rows if r["check"] == "CLI")
        auth = next(r for r in rows if r["check"] == "AUTH")
        model = next(r for r in rows if r["check"] == "MODEL")
        self.assertIn("/b/grok", cli["detail"])
        self.assertIn("DEEPSEEK_API_KEY", auth["detail"])
        self.assertIn("(open)", model["detail"])

    def test_grokbuild_open_model_requires_exported_key(self):
        p = all_green_probes()
        p.models_map["grokbuild"] = ({}, {"deepseek-v4-flash": {"model_id": "deepseek-v4-flash"}})
        rows, ok = doctor.evaluate(["grokbuild"], "deepseek-v4-flash", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertFalse(auth["ok"])
        self.assertIn("export DEEPSEEK_API_KEY", auth["detail"])


class TestExitCode(unittest.TestCase):
    def test_main_returns_zero_when_all_ok(self):
        # main() builds a real Probes(); patch the class to our fake.
        original = doctor.Probes
        doctor.Probes = lambda: all_green_probes()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = doctor.main(["--harness", "codex,pi"])
        finally:
            doctor.Probes = original
        self.assertEqual(rc, 0)

    def test_main_returns_nonzero_on_failure(self):
        p = all_green_probes()
        del p.which_map["pi"]
        original = doctor.Probes
        doctor.Probes = lambda: p
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = doctor.main(["--harness", "codex,pi"])
        finally:
            doctor.Probes = original
        self.assertEqual(rc, 1)

    def test_docker_env_main_returns_zero_on_pass(self):
        p = all_green_probes()
        p.env_map["DEEPSEEK_API_KEY"] = "sk-ds-test"
        p.exists_set.add(os.path.expanduser("~/.pi/agent/auth.json"))
        p.exists_set.add(os.path.expanduser("~/.config/devin"))
        p.run_map[("opencode", "auth", "list")] = (0, "OpenAI oauth\n")
        p.run_map[("cursor-agent", "status")] = (0, "Logged in\n")
        p.json_map[os.path.expanduser("~/.pi/agent/auth.json")] = {"openai-codex": {}}
        p.run_map[("docker", "info", "--format", "{{.NCPU}}")] = (0, "8")
        p.run_map[("docker", "image", "inspect", "test-img@sha256:abc")] = (0, "exists")
        p.run_map[("docker", "run", "--rm", "test-img@sha256:abc", "python3", "-c", "print('ok')")] = (0, "ok\n")
        original = doctor.Probes
        doctor.Probes = lambda: p
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = doctor.main(["--docker-env"])
        finally:
            doctor.Probes = original
        self.assertEqual(rc, 0)

    def test_docker_env_main_returns_nonzero_on_fail(self):
        p = all_green_probes()
        del p.which_map["docker"]
        original = doctor.Probes
        doctor.Probes = lambda: p
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = doctor.main(["--docker-env"])
        finally:
            doctor.Probes = original
        self.assertEqual(rc, 1)


class TestDockerInformational(unittest.TestCase):
    def test_image_status_reports_match_and_drift(self):
        p = all_green_probes()
        pins = doctor.pinned_versions()
        ok, detail = doctor.check_image_versions(p, ["codex", "pi"], pins)
        self.assertTrue(ok)
        self.assertIn("matches Dockerfile pins", detail)

        inspect = ("docker", "inspect", "--format", "{{json .Config.Labels}}",
                   "openbench-harness:latest")
        p.run_map[inspect] = (0, json.dumps({
            "org.openbench.cli.codex": "0.144.5",
            "org.openbench.cli.pi": "0.80.6",
        }))
        ok, detail = doctor.check_image_versions(p, ["codex", "pi"], pins)
        self.assertFalse(ok)
        self.assertIn("pi: image=0.80.6 pin=0.80.10", detail)

    def test_image_missing_reports_build_hint(self):
        p = all_green_probes()
        inspect = ("docker", "inspect", "--format", "{{json .Config.Labels}}",
                   "openbench-harness:latest")
        p.run_map[inspect] = (1, "no such image")
        ok, detail = doctor.check_image_versions(p, ["codex"], doctor.pinned_versions())
        self.assertIsNone(ok)
        self.assertIn("docker build -t openbench-harness:latest obench/docker", detail)

    def test_image_drift_fails_doctor(self):
        p = all_green_probes()
        inspect = ("docker", "inspect", "--format", "{{json .Config.Labels}}",
                   "openbench-harness:latest")
        p.run_map[inspect] = (0, json.dumps({
            "org.openbench.cli.codex": "0.144.0",
        }))
        original = doctor.Probes
        doctor.Probes = lambda: p
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = doctor.main(["--harness", "codex"])
        finally:
            doctor.Probes = original
        self.assertEqual(rc, 1)

    def test_docker_down_does_not_affect_exit(self):
        p = all_green_probes()
        # docker present but daemon not responding -> INFO FAIL, exit still 0.
        p.run_map[("docker", "info", "--format", "{{.ServerVersion}}")] = (1, "")
        original = doctor.Probes
        doctor.Probes = lambda: p
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = doctor.main(["--harness", "codex"])
        finally:
            doctor.Probes = original
        self.assertEqual(rc, 0)

    def test_check_docker_missing_is_none(self):
        p = all_green_probes()
        del p.which_map["docker"]
        ok, detail = doctor.check_docker(p)
        self.assertIsNone(ok)
        self.assertIn("informational", detail)


class TestDockerEnv(unittest.TestCase):
    """Tests for the --docker-env preflight gate."""

    def _docker_env_probes(self, **overrides):
        """Base FakeProbes with all --docker-env checks passing."""
        p = FakeProbes(
            which_map={"docker": "/b/docker"},
            run_map={
                ("docker", "buildx", "version"): (0, "buildx 0.20.0 (docker/bundle)"),
                ("docker", "info", "--format", "{{.ServerVersion}}"): (0, "27.0"),
                ("docker", "info", "--format", "{{.NCPU}}"): (0, "8"),
                ("docker", "info", "--format", "{{.MemTotal}}"): (0, "17179869184"),
                ("opencode", "auth", "list"): (0, "OpenAI oauth\n"),
                ("cursor-agent", "status"): (0, "Logged in\n"),
            },
            exists_set={os.path.expanduser("~/.codex/auth.json"),
                        os.path.expanduser("~/.pi/agent/auth.json"),
                        os.path.expanduser("~/.config/devin")},
            env_map={"ANTHROPIC_API_KEY": "sk-test", "OPENAI_API_KEY": "sk-test",
                     "DEEPSEEK_API_KEY": "sk-ds-test"},
            json_map={os.path.expanduser("~/.pi/agent/auth.json"): {"openai-codex": {}}},
        )
        for key, val in overrides.items():
            setattr(p, key, val)
        return p

    def test_check_buildx_found(self):
        p = self._docker_env_probes()
        ok, detail = doctor.check_buildx(p)
        self.assertTrue(ok)
        self.assertIn("buildx", detail.lower())

    def test_check_buildx_missing_fails(self):
        p = self._docker_env_probes()
        del p.which_map["docker"]
        ok, detail = doctor.check_buildx(p)
        self.assertFalse(ok)
        self.assertIn("not on PATH", detail)

    def test_check_buildx_plugin_not_found(self):
        p = self._docker_env_probes()
        p.run_map[("docker", "buildx", "version")] = (1, "")
        ok, detail = doctor.check_buildx(p)
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_check_docker_resources_meets_requirements(self):
        p = self._docker_env_probes()
        ok, detail = doctor.check_docker_resources(p, {"cpus": 4, "memory_gib": 12})
        self.assertTrue(ok)
        self.assertIn("CPUs=8.0", detail)
        self.assertIn("Memory=16.0", detail)

    def test_check_docker_resources_below_memory(self):
        p = self._docker_env_probes()
        p.run_map[("docker", "info", "--format", "{{.MemTotal}}")] = (0, "4294967296")  # 4 GiB
        ok, detail = doctor.check_docker_resources(p, {"cpus": 4, "memory_gib": 12})
        self.assertFalse(ok)
        self.assertIn("Memory", detail)

    def test_check_docker_resources_below_cpus(self):
        p = self._docker_env_probes()
        p.run_map[("docker", "info", "--format", "{{.NCPU}}")] = (0, "2")
        ok, detail = doctor.check_docker_resources(p, {"cpus": 4, "memory_gib": 12})
        self.assertFalse(ok)
        self.assertIn("CPUs", detail)

    def test_check_docker_resources_no_docker(self):
        p = self._docker_env_probes()
        del p.which_map["docker"]
        ok, detail = doctor.check_docker_resources(p, {"cpus": 4, "memory_gib": 12})
        self.assertFalse(ok)
        self.assertIn("not on PATH", detail)

    def test_check_task_images_present_and_functional(self):
        p = self._docker_env_probes()
        images = {
            "img1": [{"ref": "img1@sha256:abc", "source": "org/pack:1/task1"}],
            "img2": [{"ref": "img2@sha256:def", "source": "org/pack:1/task2"}],
        }
        p.run_map[("docker", "image", "inspect", "img1@sha256:abc")] = (0, "exists")
        p.run_map[("docker", "run", "--rm", "img1@sha256:abc", "python3", "-c", "print('ok')")] = (0, "ok\n")
        p.run_map[("docker", "image", "inspect", "img2@sha256:def")] = (0, "exists")
        p.run_map[("docker", "run", "--rm", "img2@sha256:def", "python3", "-c", "print('ok')")] = (0, "ok\n")
        ok, detail = doctor.check_task_images(p, images)
        self.assertTrue(ok)
        self.assertIn("2/2", detail)

    def test_check_task_images_missing_image(self):
        p = self._docker_env_probes()
        images = {
            "img1": [{"ref": "img1@sha256:abc", "source": "org/pack:1/task1"}],
        }
        p.run_map[("docker", "image", "inspect", "img1@sha256:abc")] = (1, "not found")
        ok, detail = doctor.check_task_images(p, images)
        self.assertFalse(ok)
        self.assertIn("not found locally", detail)

    def test_check_task_images_functional_probe_failure(self):
        """Functional probe catches corrupt images that inspect passes."""
        p = self._docker_env_probes()
        images = {
            "img1": [{"ref": "img1@sha256:abc", "source": "org/pack:1/task1"}],
        }
        p.run_map[("docker", "image", "inspect", "img1@sha256:abc")] = (0, "exists")
        p.run_map[("docker", "run", "--rm", "img1@sha256:abc", "python3", "-c", "print('ok')")] = (1, "exec format error")
        ok, detail = doctor.check_task_images(p, images)
        self.assertFalse(ok)
        self.assertIn("functional probe FAILED", detail)

    def test_check_task_images_empty(self):
        p = self._docker_env_probes()
        ok, detail = doctor.check_task_images(p, {})
        self.assertIsNone(ok)
        self.assertIn("n/a", detail)

    def test_check_auth_lanes_all_fresh(self):
        p = self._docker_env_probes()
        results = doctor.check_auth_lanes(p)
        # With env keys set, all lanes should pass
        all_ok = all(ok for _, ok, _ in results)
        self.assertTrue(all_ok)

    def test_check_auth_lanes_some_stale(self):
        p = self._docker_env_probes()
        p.exists_set.discard(os.path.expanduser("~/.codex/auth.json"))
        results = doctor.check_auth_lanes(p)
        codex_result = next((ok for label, ok, _ in results if "codex" in label.lower()), None)
        self.assertIsNotNone(codex_result)
        self.assertFalse(codex_result)

    def test_evaluate_docker_env_all_green(self):
        p = self._docker_env_probes()
        p.run_map[("docker", "image", "inspect", "test-img@sha256:abc")] = (0, "exists")
        p.run_map[("docker", "run", "--rm", "test-img@sha256:abc", "python3", "-c", "print('ok')")] = (0, "ok\n")
        task_images = {"test-img": [{"ref": "test-img@sha256:abc", "source": "test/1/t"}]}
        rows, ok, lane_results = doctor.evaluate_docker_env(p, task_images=task_images)
        self.assertTrue(ok)
        # BUILDX, CPUS, IMAGES, AUTH = 4 rows
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIsNot(row["ok"], False, f"{row['check']} should not be FAIL")

    def test_evaluate_docker_env_buildx_fails(self):
        p = self._docker_env_probes()
        p.run_map[("docker", "buildx", "version")] = (1, "")
        task_images = {}  # No images -> INFO
        rows, ok, lane_results = doctor.evaluate_docker_env(p, task_images=task_images)
        self.assertFalse(ok)
        buildx = next(r for r in rows if r["check"] == "BUILDX")
        self.assertFalse(buildx["ok"])

    def test_evaluate_docker_env_cpu_fails(self):
        p = self._docker_env_probes()
        p.run_map[("docker", "info", "--format", "{{.NCPU}}")] = (0, "2")
        task_images = {}
        rows, ok, lane_results = doctor.evaluate_docker_env(p, task_images=task_images)
        self.assertFalse(ok)
        cpu_row = next(r for r in rows if r["check"] == "CPUS")
        self.assertFalse(cpu_row["ok"])

    def test_load_env_requirements_defaults(self):
        p = FakeProbes()
        requirements = doctor.load_env_requirements(p, "/nonexistent")
        self.assertEqual(requirements["cpus"], 4)
        self.assertEqual(requirements["memory_gib"], 12)

    def test_load_env_requirements_from_file(self):
        p = FakeProbes()
        p.toml_map[os.path.expanduser("custom.toml")] = {"cpus": 8, "memory_gib": 24}
        requirements = doctor.load_env_requirements(p, "custom.toml")
        self.assertEqual(requirements["cpus"], 8)
        self.assertEqual(requirements["memory_gib"], 24)

    def test_discover_task_images_no_packs_dir(self):
        p = self._docker_env_probes()
        images = doctor.discover_task_images(p, "/nonexistent")
        self.assertEqual(images, {})

    def test_discover_task_images_finds_task_images(self):
        p = self._docker_env_probes()
        packs_dir = "/tmp/packs"
        p.isdir_map[packs_dir] = True
        p.isdir_map["/tmp/packs/org"] = True
        p.listdir_map[packs_dir] = ["org"]
        p.listdir_map["/tmp/packs/org"] = ["my-pack"]
        p.isdir_map["/tmp/packs/org/my-pack"] = True
        p.listdir_map["/tmp/packs/org/my-pack"] = ["1.0.0"]
        p.isdir_map["/tmp/packs/org/my-pack/1.0.0"] = True
        p.listdir_map["/tmp/packs/org/my-pack/1.0.0"] = ["task-a", "task-b"]
        p.isdir_map["/tmp/packs/org/my-pack/1.0.0/task-a"] = True
        p.isfile_map["/tmp/packs/org/my-pack/1.0.0/task-a/task.toml"] = True
        p.toml_map["/tmp/packs/org/my-pack/1.0.0/task-a/task.toml"] = {
            "docker_image": "img1@sha256:abc",
        }
        p.isdir_map["/tmp/packs/org/my-pack/1.0.0/task-b"] = True
        p.isfile_map["/tmp/packs/org/my-pack/1.0.0/task-b/task.toml"] = True
        p.toml_map["/tmp/packs/org/my-pack/1.0.0/task-b/task.toml"] = {
            "docker_image": "img2@sha256:def",
        }
        # pack.toml for pack_kind filtering
        p.isfile_map["/tmp/packs/org/my-pack/1.0.0/pack.toml"] = True
        p.toml_map["/tmp/packs/org/my-pack/1.0.0/pack.toml"] = {
            "kind": "tasks",
        }
        images = doctor.discover_task_images(p, packs_dir)
        self.assertIn("img1", images)
        self.assertIn("img2", images)
        self.assertEqual(len(images["img1"]), 1)
        self.assertEqual(images["img1"][0]["source"], "org/my-pack:1.0.0/task-a")

    def test_format_docker_env_report_contains_checks(self):
        rows = [
            {"check": "BUILDX", "ok": True, "detail": "buildx present"},
            {"check": "CPUS", "ok": True, "detail": "CPUs >= 4"},
            {"check": "IMAGES", "ok": None, "detail": "n/a"},
            {"check": "AUTH", "ok": True, "detail": "8/8 lanes fresh"},
        ]
        lane_results = [("codex", True, "path/to/auth")]
        report = doctor.format_docker_env_report(rows, lane_results, {"cpus": 4, "memory_gib": 12})
        self.assertIn("BUILDX", report)
        self.assertIn("CPUS", report)
        self.assertIn("IMAGES", report)
        self.assertIn("AUTH", report)
        self.assertIn("BUILDX", report)
        self.assertIn("CPUS", report)
        self.assertIn("IMAGES", report)
        self.assertIn("AUTH", report)
        self.assertIn("Auth lanes:", report)
        self.assertIn("codex", report)


class TestRendering(unittest.TestCase):
    def test_report_contains_status_and_details(self):
        rows, _ = doctor.evaluate(["codex"], "gpt-5.5-medium", all_green_probes())
        text = doctor.format_report(
            rows, ["codex"], (True, "daemon up"),
            (True, "openbench-harness:latest matches Dockerfile pins"))
        self.assertIn("harness", text)
        self.assertIn("CLI", text)
        self.assertIn("VERSION", text)
        self.assertIn("host=0.144.5 pin=0.144.5 [ok]", text)
        self.assertIn("Details:", text)
        self.assertIn("Docker (informational)", text)
        self.assertIn("Image pins: [OK]", text)
        self.assertIn("OK", text)


if __name__ == "__main__":
    unittest.main()
