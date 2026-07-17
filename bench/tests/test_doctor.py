#!/usr/bin/env python3
"""Unit tests for doctor.py evaluation, exit-code, and rendering logic.

All probes are mocked; no live CLI/auth/network calls are made.
"""

import contextlib
import io
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doctor  # noqa: E402


class FakeProbes:
    """Canned probe responses so evaluate() runs without touching the system.

    ``which_map``   : cli -> path|None
    ``run_map``     : tuple(argv) -> (exit_code, output)
    ``exists_set``  : set of paths (expanded) that "exist"
    ``json_map``    : path (expanded) -> parsed dict
    ``models_map``  : harness -> MODELS dict (None => import raises)
    ``env_map``     : environment variable -> value
    ``text_map``    : path (expanded) -> file text
    ``tcp_map``     : (host, port) -> reachable bool
    """

    def __init__(self, which_map=None, run_map=None, exists_set=None,
                 json_map=None, models_map=None, env_map=None, text_map=None,
                 tcp_map=None):
        self.which_map = which_map or {}
        self.run_map = run_map or {}
        self.exists_set = set(exists_set or [])
        self.json_map = json_map or {}
        self.models_map = models_map or {}
        self.env_map = env_map or {}
        self.text_map = text_map or {}
        self.tcp_map = tcp_map or {}

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

    def tcp_connect(self, host, port, timeout=1.0):
        return self.tcp_map.get((host, port), False)

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
    """A FakeProbes where every check passes for all five harnesses."""
    home = os.path.expanduser("~")
    return FakeProbes(
        which_map={"codex": "/b/codex", "pi": "/b/pi", "opencode": "/b/opencode",
                   "cursor-agent": "/b/cursor-agent", "claude": "/b/claude",
                   "grok": "/b/grok", "devin": "/b/devin", "docker": "/b/docker",
                   "cliproxyapi": "/b/cliproxyapi"},
        run_map={
            ("codex", "--version"): (0, "codex 1"),
            ("pi", "--version"): (0, "pi 1"),
            ("opencode", "--version"): (0, "opencode 1"),
            ("cursor-agent", "--version"): (0, "cursor 1"),
            ("claude", "--version"): (0, "claude 1"),
            ("grok", "--version"): (0, "grok 1"),
            ("devin", "--version"): (0, "devin 1"),
            ("opencode", "auth", "list"): (0, "OpenAI oauth\n"),
            ("cursor-agent", "status"): (0, "Logged in as x\n"),
        },
        exists_set={os.path.join(home, ".codex", "auth.json"),
                    os.path.join(home, ".pi", "agent", "auth.json"),
                    os.path.join(home, ".config", "devin")},
        json_map={os.path.join(home, ".pi", "agent", "auth.json"):
                  {"openai-codex": {}, "anthropic": {}}},
        models_map={h: {"gpt-5.5-medium": "x"} for h in doctor.ALL_HARNESSES},
        tcp_map={("127.0.0.1", 8317): True},
    )


class TestEvaluate(unittest.TestCase):
    def test_all_green_passes(self):
        rows, ok = doctor.evaluate(
            doctor.ALL_HARNESSES, "gpt-5.5-medium", all_green_probes())
        self.assertTrue(ok)
        # all harnesses x 3 checks, all OK.
        self.assertEqual(len(rows), len(doctor.ALL_HARNESSES) * 3)
        self.assertTrue(all(r["ok"] for r in rows))

    def test_grok_gpt56_requires_reachable_cliproxyapi(self):
        p = all_green_probes()
        p.models_map["grokbuild"] = ({}, {"gpt-5.6": {"model_id": "gpt-5.6"}})
        del p.which_map["cliproxyapi"]
        rows, ok = doctor.evaluate(["grokbuild"], "gpt-5.6", p)
        self.assertFalse(ok)
        auth = next(r for r in rows if r["check"] == "AUTH")
        self.assertIn("brew install cliproxyapi", auth["detail"])

        p.which_map["cliproxyapi"] = "/b/cliproxyapi"
        rows, ok = doctor.evaluate(["grokbuild"], "gpt-5.6", p)
        self.assertTrue(ok)
        self.assertIn("reachable", next(r for r in rows if r["check"] == "AUTH")["detail"])

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


class TestDockerInformational(unittest.TestCase):
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


class TestRendering(unittest.TestCase):
    def test_report_contains_status_and_details(self):
        rows, _ = doctor.evaluate(["codex"], "gpt-5.5-medium", all_green_probes())
        text = doctor.format_report(rows, ["codex"], (True, "daemon up"))
        self.assertIn("harness", text)
        self.assertIn("CLI", text)
        self.assertIn("Details:", text)
        self.assertIn("Docker (informational)", text)
        self.assertIn("OK", text)


if __name__ == "__main__":
    unittest.main()
