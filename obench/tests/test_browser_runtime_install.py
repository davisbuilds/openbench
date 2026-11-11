"""Upstream browser runtime retains full-browser configuration isolation."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[2] / 'docker/browser-runtime/install_browser_runtime.py'
browser_packages = runpy.run_path(str(INSTALLER))['browser_packages']
wrap_full_browser = runpy.run_path(str(INSTALLER))['wrap_full_browser']


class BrowserRuntimeInstallTests(unittest.TestCase):
    def test_browser_private_config_preserves_arguments_and_harness_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'chrome'
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$1"\n')
            executable.chmod(0o755)
            original = executable.read_bytes()
            provenance = wrap_full_browser(executable)
            env = dict(os.environ, XDG_CONFIG_HOME='/framework/config', XDG_CACHE_HOME='/framework/cache')
            output = subprocess.check_output([str(executable), 'argument with spaces'], env=env, text=True).splitlines()
            private_config = Path(output[0])
            try:
                self.assertEqual(private_config.parent, Path('/tmp'))
                self.assertTrue(private_config.name.startswith('openbench-chrome-config.'))
                self.assertEqual(private_config.stat().st_mode & 0o777, 0o700)
                self.assertEqual(output[1:], ['/framework/cache', 'argument with spaces'])
                self.assertEqual(Path(provenance['vendor_executable']).read_bytes(), original)
                sibling = subprocess.check_output([provenance['vendor_executable'], 'sibling'], env=env, text=True).splitlines()
                self.assertEqual(sibling, ['/framework/config', '/framework/cache', 'sibling'])
                self.assertEqual(provenance['vendor_executable_sha256'], hashlib.sha256(original).hexdigest())
            finally:
                shutil.rmtree(private_config)

    def test_cli_alpha_and_nested_stable_core_manifests_are_both_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, version, revision in [
                    ('playwright-core', '1.65.0-alpha-2026-10-30', '1252'),
                    ('playwright/node_modules/playwright-core', '1.65.0', '1254')]:
                package = root / relative
                package.mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({'version': version}))
                (package / 'browsers.json').write_text(json.dumps({'browsers': [
                    {'name': name, 'revision': revision, 'browserVersion': '158.0.8127.18'}
                    for name in ('chromium', 'chromium-headless-shell')]}))
            packages = browser_packages(root)
            self.assertEqual({item['version'] for item in packages}, {'1.65.0-alpha-2026-10-30', '1.65.0'})
            self.assertEqual({item['declared']['chromium']['revision'] for item in packages}, {'1252', '1254'})

