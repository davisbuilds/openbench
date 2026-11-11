"""CodeQL config retains maintained tools and excludes frozen task snapshots.

These are offline configuration controls; hosted CodeQL extraction and SARIF
upload still need verification after switching the repository to advanced setup.
"""

from pathlib import Path
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts.ci import verify_codeql_scope


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / '.github/codeql/codeql-config.yml'
WORKFLOW = ROOT / '.github/workflows/codeql.yml'


class CodeQLScopeTests(unittest.TestCase):
    def setUp(self):
        config = CONFIG.read_text()
        self.workflow = WORKFLOW.read_text()
        # Read only the quoted rooted-path subset used by this config. Its **
        # patterns match the paths below; this is not a general YAML parser.
        entries = config.split('paths-ignore:\n', 1)[1]
        self.ignored = []
        for line in entries.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.fullmatch(r"  - '([A-Za-z0-9_./*-]+)'", line)
            self.assertIsNotNone(match, 'unexpected scope pattern format: ' + line)
            self.ignored.append(match.group(1))
        self.assertNotRegex(config, r'(?m)^paths:')
        self.assertNotRegex(config, r'(?m)^disable-default-queries:\s*true')
        languages = re.search(r'language: \[([^\]]+)\]', self.workflow)
        self.assertIsNotNone(languages)
        self.languages = {item.strip() for item in languages.group(1).split(',')}
        self.assertIn('config-file: ./.github/codeql/codeql-config.yml', self.workflow)

    def excluded(self, path):
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.ignored)

    def test_maintained_language_controls_remain_in_scope(self):
        controls = {
            'obench/cli.py': 'python',
            'scripts/ci/unit_tests.py': 'python',
            'docker/browser-runtime/install_browser_runtime.py': 'python',
            'experiments/ablations/tools/capture_proxy.py': 'python',
            'experiments/ablations/tools/pi-deepseek-provider.mjs': 'javascript-typescript',
            'experiments/repair_cases/am123/driver.py': 'python',
            'experiments/repair_cases/am123/oracle.py': 'python',
            'benchmarks/local/am-benchmark-pr106-v2/checker_data/check.py': 'python',
            'benchmarks/local/am-benchmark-pr106-v2/checker_data/golden.test.ts': 'javascript-typescript',
            'benchmarks/candidates/job-scheduler/checker_data/run_score.py': 'python',
            'data/packs/openbench-tb-hard/dna-insert/checker_data/harbor-tests/test_outputs.py': 'python',
            '.github/workflows/codeql.yml': 'actions',
        }
        for path, language in controls.items():
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
                self.assertIn(language, self.languages)
                self.assertFalse(self.excluded(path))

    def test_frozen_payload_controls_are_excluded(self):
        for path in (
            'benchmarks/core/webcore/workspace/webcore/__init__.py',
            'benchmarks/harbor/local/am-benchmark-pr106-v4/environment/app/src/server.ts',
            'data/packs/openbench-core-smoke/make-it-run/workspace/main.py',
            'experiments/repair_cases/am123/source/src/db/schema.ts',
            'experiments/repair_cases/am123/reference/src/db/schema.ts',
            'experiments/repair_cases/am123/partial/src/db/schema.ts',
        ):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
                self.assertTrue(self.excluded(path))


class CodeQLExtractedScopeTests(unittest.TestCase):
    JS_CONTROLS = (
        'experiments/ablations/tools/pi-deepseek-provider.mjs',
        'benchmarks/local/am-benchmark-pr106-v2/checker_data/golden.test.ts',
    )

    def archive(self, database, paths, *, prefix=None):
        prefix = ROOT.as_posix().lstrip('/') + '/' if prefix is None else prefix
        with zipfile.ZipFile(database / 'src.zip', 'w') as archive:
            for path in paths:
                archive.writestr(prefix + path, 'synthetic source; content is not read')
            archive.writestr('opt/codeql/external-library.js', 'external')

    def test_archive_observes_maintained_js_and_ignores_external_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory)
            self.archive(database, self.JS_CONTROLS)
            report = verify_codeql_scope.verify_archive(database, 'javascript-typescript', ROOT)
            self.assertTrue(report['ok'])
            self.assertEqual(report['repository_files'], 2)
            self.assertEqual(report['external_files'], 1)
            self.assertTrue(report['included_controls'][self.JS_CONTROLS[0]])

    def test_missing_maintained_provider_and_leaked_frozen_snapshot_fail(self):
        frozen = 'experiments/repair_cases/am123/source/src/db/schema.ts'
        for paths in (self.JS_CONTROLS[1:], (*self.JS_CONTROLS, frozen)):
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as directory:
                database = Path(directory)
                self.archive(database, paths)
                report = verify_codeql_scope.verify_archive(database, 'javascript-typescript', ROOT)
                self.assertFalse(report['ok'])

    def test_absent_archive_and_unrecognized_archive_root_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory)
            with self.assertRaises(FileNotFoundError):
                verify_codeql_scope.verify_archive(database, 'javascript-typescript', ROOT)
            self.archive(database, self.JS_CONTROLS, prefix='different-checkout/')
            with self.assertRaisesRegex(ValueError, 'no paths rooted at checkout'):
                verify_codeql_scope.verify_archive(database, 'javascript-typescript', ROOT)

    def test_cli_reads_action_database_output_and_emits_control_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory)
            self.archive(database, self.JS_CONTROLS)
            summary = database / 'summary.md'
            env = dict(os.environ, CODEQL_LANGUAGE='javascript-typescript',
                       CODEQL_DATABASES=json.dumps({'javascript': str(database)}),
                       GITHUB_WORKSPACE=str(ROOT), GITHUB_STEP_SUMMARY=str(summary))
            command = [sys.executable, str(ROOT / 'scripts/ci/verify_codeql_scope.py')]
            result = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertIn('PASS included: `' + self.JS_CONTROLS[0] + '`', summary.read_text())
            self.archive(database, self.JS_CONTROLS[1:])
            result = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('FAIL included: `' + self.JS_CONTROLS[0] + '`', summary.read_text())


if __name__ == '__main__':
    unittest.main()
