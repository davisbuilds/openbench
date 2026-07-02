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
    """

    def __init__(self, which_map=None, run_map=None, exists_set=None,
                 json_map=None, models_map=None):
        self.which_map = which_map or {}
        self.run_map = run_map or {}
        self.exists_set = set(exists_set or [])
        self.json_map = json_map or {}
        self.models_map = models_map or {}

    def which(self, cli):
        return self.which_map.get(cli)

    def run(self, argv, timeout=15):
        return self.run_map.get(tuple(argv), (None, ""))

    def exists(self, path):
        return os.path.expanduser(path) in self.exists_set

    def read_json(self, path):
        return self.json_map.get(os.path.expanduser(path))

    def import_adapter(self, name):
        models = self.models_map.get(name)
        if models is None:
            raise FileNotFoundError(f"no adapter {name}")
        mod = types.ModuleType(f"fake_{name}")
        mod.MODELS = models
        return mod


def all_green_probes():
    """A FakeProbes where every check passes for all five harnesses."""
    home = os.path.expanduser("~")
    return FakeProbes(
        which_map={"codex": "/b/codex", "pi": "/b/pi", "opencode": "/b/opencode",
                   "cursor-agent": "/b/cursor-agent", "devin": "/b/devin",
                   "docker": "/b/docker"},
        run_map={
            ("codex", "--version"): (0, "codex 1"),
            ("pi", "--version"): (0, "pi 1"),
            ("opencode", "--version"): (0, "opencode 1"),
            ("cursor-agent", "--version"): (0, "cursor 1"),
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
    )


class TestEvaluate(unittest.TestCase):
    def test_all_green_passes(self):
        rows, ok = doctor.evaluate(
            doctor.ALL_HARNESSES, "gpt-5.5-medium", all_green_probes())
        self.assertTrue(ok)
        # 5 harnesses x 3 checks, all OK.
        self.assertEqual(len(rows), 15)
        self.assertTrue(all(r["ok"] for r in rows))

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
