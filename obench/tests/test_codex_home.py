"""Exercise Codex adapter environments with a real, offline child process.

The executable fixture reports the home-based discovery roots it receives;
it does not emulate Codex's complete skill loader or call a model.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from obench.adapters import _codex_ablation, codex


class TestCodexChildHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.parent_home = self.root / "operator"
        self.canary = self.parent_home / ".agents/skills/canary/SKILL.md"
        self.canary.parent.mkdir(parents=True)
        self.canary.write_text("operator skill canary\n")
        self.source = self.parent_home / ".codex"
        self.source.mkdir()
        self.auth = '{"account_id":"fixture","refresh_token":"old"}'
        (self.source / "auth.json").write_text(self.auth)
        (self.source / "config.toml").write_text("operator config canary\n")
        self.work = self.root / "work"
        self.work.mkdir()
        bindir = self.root / "bin"
        bindir.mkdir()
        self.executable = bindir / "codex"
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os\n"
            "from pathlib import Path\n"
            "home = Path.home()\n"
            "config = Path(os.environ['CODEX_HOME']).expanduser()\n"
            "auth = config / 'auth.json'\n"
            "report = {'home': str(home), 'codex_home': str(config),\n"
            "          'skills': sorted(str(p.relative_to(home)) for p in\n"
            "                    (home / '.agents/skills').glob('*/SKILL.md')),\n"
            "          'files': sorted(p.name for p in config.iterdir()),\n"
            "          'auth': json.loads(auth.read_text())}\n"
            "cfg = config / 'config.toml'\n"
            "if cfg.exists():\n"
            "    report['config'] = cfg.read_text()\n"
            "    instructions = config / 'instructions.md'\n"
            "    if instructions.exists():\n"
            "        report['instructions'] = instructions.read_text()\n"
            "print(json.dumps(report), flush=True)\n"
            "auth.write_text(json.dumps({'account_id': 'fixture',\n"
            "                            'refresh_token': 'rotated'}))\n"
            "if os.environ.get('FIXTURE_WAIT'):\n"
            "    import time\n"
            "    time.sleep(60)\n"
            "raise SystemExit(int(os.environ.get('FIXTURE_EXIT', '0')))\n"
        )
        self.executable.chmod(0o755)
        patch = mock.patch.dict(os.environ, {
            "HOME": str(self.parent_home), "PATH": str(bindir),
        }, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def assert_isolated(self, result):
        report = json.loads(result["full_output"])
        self.assertEqual(report["skills"], [])
        self.assertNotEqual(report["home"], str(self.parent_home))
        self.assertFalse(Path(report["home"]).exists(), "temporary HOME leaked")
        self.assertEqual(self.canary.read_text(), "operator skill canary\n")
        self.assertEqual(os.environ["HOME"], str(self.parent_home))
        self.assertEqual(report["auth"]["account_id"], "fixture")
        self.assertEqual(report["auth"]["refresh_token"], "old")
        self.assertEqual(json.loads((self.source / "auth.json").read_text())[
            "refresh_token"], "rotated")
        return report

    def test_probe_detects_parent_skills_without_isolation(self):
        env = dict(os.environ, CODEX_HOME=str(self.source))
        proc = subprocess.run([str(self.executable)], env=env,
                              capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(proc.stdout)["skills"],
                         [".agents/skills/canary/SKILL.md"])

    def test_stock_child_hides_parent_skills_and_stages_auth(self):
        result = codex.run("fixture", str(self.work), "gpt-5.5-medium", 5)
        self.assertTrue(result["completed"])
        report = self.assert_isolated(result)
        self.assertEqual(report["files"], ["auth.json"])
        self.assertFalse(Path(report["codex_home"]).exists())

    def test_failed_child_isolates_home_and_preserves_explicit_auth_source(self):
        with mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.source), "FIXTURE_EXIT": "1",
        }):
            result = codex.run("fixture", str(self.work), "gpt-5.5-medium", 5,
                               env_override={"HOME": str(self.parent_home)})
        self.assertFalse(result["completed"])
        report = self.assert_isolated(result)
        self.assertFalse(Path(report["codex_home"]).exists())

    def test_supplied_codex_home_keeps_config_but_isolates_user_home(self):
        result = codex.run("fixture", str(self.work), "gpt-5.5-medium", 5,
                           env_override={"CODEX_HOME": "~/.codex"})
        self.assertTrue(result["completed"])
        report = self.assert_isolated(result)
        self.assertEqual(report["codex_home"], str(self.source))
        self.assertEqual(report["config"], "operator config canary\n")
        self.assertTrue(self.source.exists(), "caller-owned home was removed")

    def test_timeout_cleans_home_and_persists_auth_rotation(self):
        with mock.patch.dict(os.environ, {"FIXTURE_WAIT": "1"}):
            result = codex.run("fixture", str(self.work), "gpt-5.5-medium", 1)
        self.assertEqual(result["error"], "timeout after 1s")
        report = self.assert_isolated(result)
        self.assertFalse(Path(report["codex_home"]).exists())

    def test_ablation_keeps_composed_instructions_with_isolated_user_home(self):
        variant = self.root / "ablation/codex-home-v1"
        variant.mkdir(parents=True)
        (variant / "config.toml").write_text(
            'model_instructions_file = "instructions.md"\n'
            'include_environment_context = false\n')
        (variant / "instructions.md").write_text("fixture ablation instructions\n")
        with mock.patch.object(_codex_ablation, "_ablation_root",
                               return_value=str(variant.parent)):
            result = _codex_ablation.run_variant(
                "codex_v1", "v1", "fixture", str(self.work), "gpt-5.5-medium", 5)
        self.assertTrue(result["completed"])
        report = self.assert_isolated(result)
        self.assertEqual(report["instructions"], "fixture ablation instructions\n")
        self.assertIn('include_environment_context = false', report["config"])
        self.assertIn(str(Path(report["codex_home"]) / "instructions.md"),
                      report["config"])
        self.assertFalse(Path(report["codex_home"]).exists())


if __name__ == "__main__":
    unittest.main()
