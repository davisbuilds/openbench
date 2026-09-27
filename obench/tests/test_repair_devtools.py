import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from obench.repair_devtools import initialize_git


class WorkspaceGitTests(unittest.TestCase):
    def test_clean_baseline_diff_and_no_original_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'app'
            app.mkdir()
            (app / 'source.py').write_text('answer = 0\n')
            (app / '.gitignore').write_text('*.py\n')
            initialize_git(app, root / 'history')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(app), *args], text=True)
            self.assertEqual(git('rev-list', '--count', 'HEAD').strip(), '1')
            self.assertEqual(git('remote'), '')
            self.assertEqual(git('status', '--porcelain'), '')
            self.assertEqual(git('show', 'HEAD:source.py'), 'answer = 0\n')
            (app / 'source.py').write_text('answer = 1\n')
            self.assertIn('+answer = 1', git('diff'))
            self.assertTrue((app / '.git').is_file())
            self.assertEqual((app / '.git').read_text(), f'gitdir: {(root / "history").resolve()}\n')
            self.assertFalse(list((root / 'history/hooks').glob('*')))

    def test_refuses_supplied_git_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            (app / '.git').mkdir()
            with self.assertRaisesRegex(ValueError, 'history'):
                initialize_git(app, app / 'history')


class DeveloperCaseTests(unittest.TestCase):
    def test_packaged_cases_preserve_buggy_source_and_oracles(self):
        import tomllib
        from obench.sandbox_grading import task_digest
        from obench.repair_oracles.registry import task_digest as registered_digest
        root = Path(__file__).resolve().parents[2] / 'benchmarks/harbor/local'
        for previous, current, prefix in [
            ('dojo-evidence-pr60-v5', 'dojo-evidence-pr60-c3-o5', 'scripts/profiles'),
            ('am-benchmark-pr106-v4', 'am-benchmark-pr106-c2-o3', 'src'),
        ]:
            with self.subTest(case=current):
                def sources(name):
                    source = root / name / 'environment/app' / prefix
                    return {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
                self.assertEqual(sources(previous), sources(current))
                old = tomllib.loads((root / previous / 'task.toml').read_text())['metadata']
                new = tomllib.loads((root / current / 'task.toml').read_text())['metadata']
                self.assertEqual(old['openbench_revision']['oracle_revision'], new['openbench_revision']['oracle_revision'])
                self.assertEqual(new['openbench_revision']['case_revision'], old['openbench_revision']['case_revision'] + 1)
                digest = registered_digest if new.get('openbench_oracle') else task_digest
                self.assertEqual(digest(root / current), new['openbench_task_content_digest']['sha256'])
                self.assertTrue(list((root / current / 'environment/app/tests').glob('*')))
