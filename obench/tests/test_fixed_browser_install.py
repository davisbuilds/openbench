"""Fixed browser inputs authenticate bytes and preserve executable layout."""
import hashlib
import json
from pathlib import Path
import runpy
import stat
import tempfile
import unittest
import zipfile


INSTALLER = Path(__file__).resolve().parents[2] / 'docker/browser-runtime/install_fixed_browser.py'
extract = runpy.run_path(str(INSTALLER))['extract_verified_archive']
browser_packages = runpy.run_path(str(INSTALLER))['browser_packages']


class FixedBrowserInstallTests(unittest.TestCase):
    def test_cli_alpha_and_nested_stable_core_manifests_are_both_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, version, revision in [
                    ('playwright-core', '1.64.0-alpha-1790635538000', '1247'),
                    ('playwright/node_modules/playwright-core', '1.64.0', '1248')]:
                package = root / relative
                package.mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({'version': version}))
                (package / 'browsers.json').write_text(json.dumps({'browsers': [
                    {'name': name, 'revision': revision, 'browserVersion': '156.0.8078.4'}
                    for name in ('chromium', 'chromium-headless-shell')]}))
            packages = browser_packages(root)
            self.assertEqual({item['version'] for item in packages}, {'1.64.0-alpha-1790635538000', '1.64.0'})
            self.assertEqual({item['declared']['chromium']['revision'] for item in packages}, {'1247', '1248'})

    def make_archive(self, root, name='chrome-linux64/chrome', mode=stat.S_IFREG | 0o755):
        archive = root / 'browser.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            entry = zipfile.ZipInfo(name)
            entry.external_attr = mode << 16
            output.writestr(entry, b'fixed browser fixture')
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_verified_archive_preserves_expected_path_contents_and_execute_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest = self.make_archive(root)
            extract(archive, digest, root / 'installed', 'chrome-linux64')
            installed = root / 'installed/chrome-linux64/chrome'
            self.assertEqual(installed.read_bytes(), b'fixed browser fixture')
            self.assertEqual(installed.stat().st_mode & 0o777, 0o755)

    def test_plausible_archive_with_wrong_digest_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, _ = self.make_archive(root)
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                extract(archive, 'a' * 64, root / 'installed', 'chrome-linux64')
            self.assertFalse((root / 'installed').exists())

    def test_authenticated_unexpected_paths_and_symlinks_are_rejected(self):
        for name, mode in [('chrome-linux64/../../outside', stat.S_IFREG | 0o644),
                           ('other-browser/chrome', stat.S_IFREG | 0o755),
                           ('/chrome-linux64/chrome', stat.S_IFREG | 0o755),
                           ('chrome-linux64/link', stat.S_IFLNK | 0o777)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive, digest = self.make_archive(root, name, mode)
                with self.assertRaisesRegex(ValueError, 'Unexpected'):
                    extract(archive, digest, root / 'installed', 'chrome-linux64')
                self.assertFalse((root / 'installed').exists())
