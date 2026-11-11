"""The browser-quality CI classifier derives inputs from source and fails closed."""
import importlib.util
import io
import os
from contextlib import redirect_stdout
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'browser_quality_inputs', ROOT / 'scripts/ci/browser_quality_inputs.py')
bqi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bqi)

WORKFLOW = """name: Harbor Integration
jobs:
  classify:
    runs-on: ubuntu-24.04
    steps:
      - run: python3 scripts/ci/classify_only.py
  integration:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v0
      - name: Install
        run: uv pip sync deps/lock.txt
      - name: Other lane only
        if: matrix.lane == 'repair-grading'
        run: python3 scripts/local/other.py
      - name: Browser variants
        if: startsWith(matrix.lane, 'browser')
        run: python3 scripts/local/build.py
      - name: Lane control
        if: matrix.lane == 'browser-quality'
        run: >-
          .venv/bin/python scripts/local/entry.py
          --task tasks/app
  sandbox:
    needs: integration
    steps:
      - run: python3 scripts/local/gate_only.py
"""

FILES = {
    '.github/workflows/harbor-integration.yml': WORKFLOW,
    'scripts/ci/browser_quality_inputs.py': '# classifier\n',
    'scripts/ci/classify_only.py': '',
    'scripts/ci/fixtures.py': 'import json\n',
    'scripts/local/other.py': 'import obench.unrelated\n',
    'scripts/local/gate_only.py': '',
    'scripts/local/build.py': "CONTEXT = 'docker/runtime'\n",
    'scripts/local/entry.py': ('import sys\nfrom fixtures import prepare\nfrom obench import a\n'
                               'from obench.repair_oracles import registry\n'),
    'deps/lock.txt': 'harbor==1\n',
    'docker/runtime/Dockerfile': 'FROM scratch\n',
    'docker/unused/Dockerfile': 'FROM scratch\n',
    'tasks/app/task.toml': '',
    'tasks/other/task.toml': '',
    'obench/__init__.py': '',
    'obench/a.py': ("from . import b\nWORKER = Path(__file__).parent / 'worker.py'\n"
                    "SECCOMP = 'seccomp.json'\nSNIPPET = 'import obench.inline'\n"),
    'obench/b.py': '',
    'obench/worker.py': '',
    'obench/inline.py': '',
    'obench/assets/seccomp.json': '{}\n',
    'obench/unrelated.py': '# outside the lane\n',
    'obench/repair_oracles/__init__.py': '',
    'obench/repair_oracles/registry.py': "LOADED = 'obench.repair_oracles.v1'\n",
    'obench/repair_oracles/v1.py': '',
    'obench/repair_oracles/v2.py': '# loaded by name\n',
    'obench/adapters/__init__.py': '',
    'obench/adapters/x.py': '',
    'docs/guide.md': 'see ./docs\n',
}


class Repo:
    def __init__(self, path):
        self.path = path
        self.git('init', '-q', '-b', 'main')
        self.write(FILES)
        self.base = self.commit('base')

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        return subprocess.run(['git', '-C', str(self.path), '-c', 'commit.gpgsign=false', *args],
                              check=True, capture_output=True, text=True, env=env).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def change(self, files=(), delete=()):
        self.git('checkout', '-q', '-B', 'topic', self.base)
        self.write({name: FILES.get(name, '') + '# changed\n' for name in files})
        for name in delete:
            (self.path / name).unlink()
        return self.commit('change')

    def decide(self, head):
        return bqi.decide(str(self.path), self.base, head)


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repo(Path(directory.name))

    def assertRuns(self, files=(), delete=(), matched=None):
        report = self.repo.decide(self.repo.change(files, delete))
        self.assertTrue(report['run'], report)
        if matched is not None:
            self.assertEqual(report['matched'], matched)

    def assertSkips(self, files):
        report = self.repo.decide(self.repo.change(files))
        self.assertFalse(report['run'], report)
        self.assertEqual(report['matched'], [])
        self.assertEqual(report['changed'], sorted(files))

    def test_changes_outside_the_closure_skip(self):
        self.assertSkips(['obench/unrelated.py', 'docs/guide.md', 'docker/unused/Dockerfile',
                          'tasks/other/task.toml', 'scripts/local/other.py',
                          'scripts/local/gate_only.py', 'scripts/ci/classify_only.py'])

    def test_each_kind_of_derived_input_runs(self):
        for path in ('scripts/local/entry.py', 'scripts/local/build.py', 'deps/lock.txt',
                     'tasks/app/task.toml', 'obench/__init__.py', 'obench/b.py', 'obench/worker.py',
                     'obench/assets/seccomp.json', 'obench/inline.py', 'scripts/ci/fixtures.py',
                     'docker/runtime/Dockerfile', 'obench/repair_oracles/v2.py', 'obench/repair_oracles/registry.py',
                     '.github/workflows/harbor-integration.yml', 'scripts/ci/browser_quality_inputs.py'):
            with self.subTest(path=path):
                self.assertRuns([path], matched=[path])

    def test_unreached_dynamic_family_is_not_an_input(self):
        self.assertSkips(['obench/adapters/x.py'])

    def test_a_deleted_input_still_runs(self):
        # Deleting b.py breaks a.py without changing it; the base closure
        # still names b.py.
        self.assertRuns(delete=['obench/b.py'], matched=['obench/b.py'])

    def test_a_new_import_of_an_unrelated_module_runs(self):
        self.repo.change(['obench/unrelated.py'])
        self.repo.write({'obench/b.py': 'from . import unrelated\n'})
        report = self.repo.decide(self.repo.commit('import'))
        self.assertTrue(report['run'])
        self.assertEqual(report['matched'], ['obench/b.py', 'obench/unrelated.py'])

    def test_a_dependency_added_on_the_advancing_base_runs(self):
        # The PR changes only unrelated.py, but main has since started
        # importing it; the merged code CI tests reaches the change.
        head = self.repo.change(['obench/unrelated.py'])
        self.repo.git('checkout', '-q', 'main')
        self.repo.write({'obench/b.py': 'from . import unrelated\n'})
        advanced = self.repo.commit('main imports unrelated')
        report = bqi.decide(str(self.repo.path), advanced, head)
        self.assertTrue(report['run'], report)
        self.assertEqual(report['matched'], ['obench/unrelated.py'])

    def test_the_tested_merge_revision_contributes_inputs(self):
        head = self.repo.change(['obench/unrelated.py'])
        self.repo.git('checkout', '-q', '-B', 'merged', head)
        self.repo.write({'obench/b.py': 'from . import unrelated\n'})
        merged = self.repo.commit('merge result')
        self.assertFalse(self.repo.decide(head)['run'])
        report = bqi.decide(str(self.repo.path), self.repo.base, head, merged)
        self.assertTrue(report['run'], report)
        with self.assertRaises(ValueError):
            bqi.decide(str(self.repo.path), self.repo.base, head, 'HEAD')

    def test_unknown_layout_fails_closed(self):
        self.repo.git('checkout', '-q', '-B', 'topic', self.repo.base)
        self.repo.write({'.github/workflows/harbor-integration.yml':
                         WORKFLOW.replace("matrix.lane == 'browser-quality'", "matrix.lane != 'browser'")})
        layout = self.repo.commit('layout')
        self.repo.base = layout
        with self.assertRaisesRegex(ValueError, "conditioned on matrix.lane == 'browser-quality'"):
            self.repo.decide(self.repo.change(['obench/unrelated.py']))

    def test_lane_step_without_a_repository_path_fails_closed(self):
        self.repo.git('checkout', '-q', '-B', 'topic', self.repo.base)
        self.repo.write({'.github/workflows/harbor-integration.yml':
                         WORKFLOW.replace('scripts/local/entry.py', 'entry.py').replace('tasks/app', 'app')})
        self.repo.base = self.repo.commit('relative')
        with self.assertRaisesRegex(ValueError, 'names no repository path'):
            self.repo.decide(self.repo.change(['obench/unrelated.py']))

    def test_invalid_comparisons_fail_closed(self):
        head = self.repo.change(['obench/unrelated.py'])
        for base, head_ in ((self.repo.base[:12], head), ('0' * 40, head), (self.repo.base, None),
                            (self.repo.base, self.repo.base)):
            with self.subTest(base=base, head=head_), self.assertRaises(Exception):
                bqi.decide(str(self.repo.path), base, head_)

    def test_shallow_history_fails_closed(self):
        head = self.repo.change(['obench/unrelated.py'])
        with tempfile.TemporaryDirectory() as clone:
            subprocess.run(['git', 'clone', '-q', '--depth', '1', '--branch', 'topic',
                            self.repo.path.as_uri(), clone], check=True, capture_output=True)
            with self.assertRaisesRegex(ValueError, 'shallow'):
                bqi.decide(clone, self.repo.base, head)

    def test_cli_prints_skip_last_and_runs_on_any_error(self):
        head = self.repo.change(['obench/unrelated.py'])
        cases = (([self.repo.base, head], 'run=false'), ([self.repo.base, 'not-a-sha'], 'run=true'),
                 (['0' * 40, head], 'run=true'))
        for (base, head_), last in cases:
            with self.subTest(head=head_):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = bqi.main(['--repo', str(self.repo.path), '--base', base, '--head', head_])
                lines = output.getvalue().splitlines()
                self.assertEqual(status, 0)
                self.assertEqual(lines[-1], last)
                self.assertEqual(json.loads(lines[0])['run'], last == 'run=true')


class RealWorkflowTests(unittest.TestCase):
    """Known positives and negatives on this checkout's actual lane."""

    @classmethod
    def setUpClass(cls):
        try:
            tree = bqi.WorkTree(str(ROOT))
        except (OSError, subprocess.CalledProcessError) as error:
            raise unittest.SkipTest(f'needs a Git checkout: {error}')
        cls.files, cls.directories = bqi.inputs(tree)

    def covered(self, path):
        return bqi.matches(path, self.files, self.directories)

    def test_lane_inputs_are_covered(self):
        graders = ['package_campaign', 'evaluation_package', 'repair_validation', 'sandbox_grading',
                   'repair_grading', 'repair_worker', 'repair_identity', 'harbor_sandbox',
                   'repair_evidence', 'repair_workflow']
        expected = [f'obench/{name}.py' for name in graders] + [
            'obench/repair_oracles/registry.py', 'obench/sandbox_gateway.py',
            'obench/repair_devtools.py', 'obench/runtime_trust.py', 'obench/browser-seccomp.json',
            'scripts/local/verify_browser_quality.py', 'scripts/local/verify_repair_codex.py',
            'scripts/ci/browser_quality_fixtures.py', 'scripts/local/build_repair_runtime.py',
            'scripts/local/build_browser_runtime.py', 'docker/repair-sandbox/Dockerfile',
            'docker/repair-sandbox/node/package-lock.json', 'docker/browser-runtime/Dockerfile',
            'docker/browser-runtime/package-lock.json', '.github/requirements/harbor.txt',
            '.github/requirements/harbor-build.txt', '.github/workflows/harbor-integration.yml',
            'benchmarks/harbor/local/activity-explorer-c2-o2/task.toml',
            'scripts/ci/browser_quality_inputs.py']
        expected += [str(p.relative_to(ROOT)) for p in (ROOT / 'obench/repair_oracles').glob('*.py')]
        self.assertEqual([p for p in expected if not self.covered(p)], [])

    def test_other_lane_and_reporting_sources_are_not(self):
        for path in ('obench/report.py', 'obench/cli.py', 'obench/browser_review.py',
                     'scripts/local/verify_browser_lifecycle.py', 'docs/project/CI.md',
                     'benchmarks/harbor/local/dojo-evidence-pr60-v5/task.toml'):
            with self.subTest(path=path):
                self.assertFalse(self.covered(path))

    def test_browser_download_inputs_and_future_context_files_are_covered(self):
        self.assertIn('docker/browser-runtime', self.directories)
        for path in ('docker/browser-runtime/install_browser_runtime.py',
                     'docker/browser-runtime/future-assets/browser-manifest.json'):
            with self.subTest(path=path):
                self.assertTrue(self.covered(path))


if __name__ == '__main__':
    unittest.main()
