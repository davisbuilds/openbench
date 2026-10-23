"""Exercise the real shell entry point without Python in the bridge environment."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'openmodel_bridge.sh'


class BridgeInstallationTests(unittest.TestCase):
    def run_bridge(self, version='1.106.2', marker=None, metadata=True, declared_version=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'bridge-venv/bin'
            binary.mkdir(parents=True)
            site = root / 'bridge-venv/lib/python3.12/site-packages'
            site.mkdir(parents=True)
            dist = site / f'litellm-{version}.dist-info'
            dist.mkdir()
            if metadata:
                (dist / 'METADATA').write_text(f'Name: litellm\nVersion: {declared_version or version}\n')
            if marker:
                path = site / marker
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic control, never execute\n')
            for name in ['litellm', 'python']:
                path = binary / name
                path.write_text(f'#!/bin/sh\ntouch "{root}/{name}-executed"\n')
                path.chmod(0o755)
            (root / 'keys.env').write_text(f'touch "{root}/keys-loaded"\n')
            result = subprocess.run(['bash', str(SCRIPT)], env={**os.environ, 'OPENBENCH_HOME': str(root)},
                                    capture_output=True, text=True, timeout=5)
            return result, {name: (root / name).exists() for name in
                            ['litellm-executed', 'python-executed', 'keys-loaded']}

    def test_safe_installation_reaches_key_loading_and_proxy(self):
        result, effects = self.run_bridge()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(effects['keys-loaded'])
        self.assertTrue(effects['litellm-executed'])
        self.assertFalse(effects['python-executed'])

    def test_compromised_version_is_blocked_before_keys_or_execution(self):
        result, effects = self.run_bridge(version='1.107.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(effects.values()), effects)
        self.assertIn('rotate', result.stderr)

    def test_metadata_version_and_ambiguous_records_cannot_bypass_the_guard(self):
        for version in ['1.107.0', '1.107.0+local', '1.106.2\nVersion: 1.107.0']:
            result, effects = self.run_bridge(declared_version=version)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(effects.values()), effects)

    def test_startup_indicators_and_missing_metadata_fail_closed(self):
        for marker in ['cook.pth', 'cook.py', 'sitecustomize.py',
                       '__pycache__/sitecustomize.cpython-312.pyc', '__pycache__/cook.cpython-312.pyc']:
            with self.subTest(marker=marker):
                result, effects = self.run_bridge(marker=marker)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(effects.values()), effects)
        result, effects = self.run_bridge(metadata=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(effects.values()), effects)


if __name__ == '__main__':
    unittest.main()
