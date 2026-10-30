import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from obench.repair_devtools import initialize_git, initialize_workspace


class WorkspaceGitTests(unittest.TestCase):
    def test_actual_initialization_supports_writable_dependency_scratch_without_archive_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'app'
            app.mkdir()
            (app / 'package.json').write_text('{"name":"example"}')
            initialize_workspace(app, root / 'history')
            scratch = Path(tempfile.mkdtemp(prefix='.tmp-cli-', dir=app / 'node_modules'))
            (scratch / 'package.json').write_text('{}')
            self.assertFalse(any(p.is_symlink() for p in app.rglob('*')))
            self.assertEqual(subprocess.check_output(['git', '-C', str(app), 'status', '--porcelain'], text=True), '')
            self.assertEqual(subprocess.check_output(['git', '-C', str(app), 'rev-list', '--count', 'HEAD'], text=True).strip(), '1')

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

    def test_review_has_real_base_candidate_diff_and_no_other_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'keep.py').write_text('value = 1\n')
            (source / 'deleted').write_text('before\n')
            initialize_git(source, root / 'source-history')
            (source / 'keep.py').write_text('value = 2\n')
            (source / 'deleted').unlink()
            (source / 'new.bin').write_bytes(b'\x00\x01new')
            (source / 'executable').write_text('#!/bin/sh\n')
            (source / 'executable').chmod(0o755)
            subprocess.run(['git', '-C', str(source), 'add', '--all'], check=True)
            patch = subprocess.check_output(['git', '-C', str(source), 'diff', '--cached', '--binary', '--full-index'])
            app = root / 'app'
            app.mkdir()
            for name in ('keep.py', 'new.bin', 'executable'):
                (app / name).write_bytes((source / name).read_bytes())
                (app / name).chmod((source / name).stat().st_mode & 0o777)
            (app / 'review.patch').write_bytes(patch)
            (app / '.openbench-review.json').write_text(json.dumps({'schema': 1, 'patch': 'review.patch'}))
            initialize_workspace(app, root / 'history')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(app), *args])
            self.assertEqual(git('rev-list', '--count', 'HEAD').strip(), b'2')
            self.assertEqual(git('show', 'review-base:keep.py'), b'value = 1\n')
            self.assertEqual(git('show', 'HEAD:keep.py'), b'value = 2\n')
            self.assertEqual(git('diff', '--binary', '--full-index', 'review-base', 'HEAD'), patch)
            self.assertEqual(git('status', '--porcelain'), b'')
            self.assertEqual(git('remote'), b'')
            self.assertEqual(git('rev-parse', 'review-candidate'), git('rev-parse', 'HEAD'))
            self.assertEqual((app / 'new.bin').read_bytes(), b'\x00\x01new')
            self.assertFalse((app / 'deleted').exists())

    def test_review_rejects_invalid_or_outside_patch(self):
        for spec, patch in [({'schema': 1, 'patch': '../outside'}, b'bad'),
                            ({'schema': 1, 'patch': 'review.patch'}, b'bad')]:
            with self.subTest(spec=spec), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                app = root / 'app'
                app.mkdir()
                (app / 'review.patch').write_bytes(patch)
                (app / '.openbench-review.json').write_text(json.dumps(spec))
                with self.assertRaises((ValueError, RuntimeError)):
                    initialize_workspace(app, root / 'history')

    def test_refuses_supplied_git_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            (app / '.git').mkdir()
            with self.assertRaisesRegex(ValueError, 'history'):
                initialize_git(app, app / 'history')


class DeveloperCaseTests(unittest.TestCase):
    def test_patched_runtime_control_preserves_historical_case_except_pnpm_engine(self):
        import difflib
        import hashlib
        import tomllib
        from obench.repair_oracles.registry import task_digest
        from obench import runtime_admission
        root = Path(__file__).resolve().parents[2]
        tasks = root / 'benchmarks/harbor/local'
        previous = tasks / 'am-benchmark-pr106-c2-o3'
        current = tasks / 'am-benchmark-pr106-c3-o3'
        def files(task):
            app = task / 'environment/app'
            return {p.relative_to(app).as_posix(): p.read_bytes()
                    for p in app.rglob('*') if p.is_file()}
        old, new = files(previous), files(current)
        before = old.pop('package.json')
        after = new.pop('package.json')
        self.assertEqual(old, new)
        self.assertEqual(after, before.replace(b'"pnpm": "10.29.3"', b'"pnpm": "10.34.7"', 1))
        self.assertEqual(json.loads(before)['engines']['pnpm'], '10.29.3')
        self.assertEqual(json.loads(after)['engines']['pnpm'], '10.34.7')
        evidence = current / 'execution-revision'
        self.assertEqual((evidence / 'package.original.json').read_bytes(), before)
        patch = ''.join(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
            fromfile='a/environment/app/package.json', tofile='b/environment/app/package.json')).encode()
        self.assertEqual((evidence / 'package.patch').read_bytes(), patch)
        manifest = json.loads((evidence / 'manifest.json').read_text())
        for key, content in [('original_sha256', before), ('revised_sha256', after),
                             ('patch_sha256', patch), ('source_task_toml_sha256', (previous / 'task.toml').read_bytes())]:
            self.assertEqual(manifest[key], hashlib.sha256(content).hexdigest())
        source = json.loads((current / 'SOURCE_MANIFEST.json').read_text())
        entry = next(item for item in source['environment/app'] if item['path'] == 'package.json')
        self.assertEqual(entry['sha256'], hashlib.sha256(after).hexdigest())
        old_metadata = tomllib.loads((previous / 'task.toml').read_text())['metadata']
        metadata = tomllib.loads((current / 'task.toml').read_text())['metadata']
        self.assertEqual(metadata['openbench_revision'], {**old_metadata['openbench_revision'], 'case_revision': 3})
        self.assertEqual(task_digest(current), metadata['openbench_task_content_digest']['sha256'])
        # The selected developer control and every retained revision remain bound
        # into admission; changing the fixture cannot reuse an old receipt.
        import runpy
        selected = runpy.run_path(str(root / 'scripts/local/verify_repair_developer.py'))['TASKS']
        self.assertIn(current.name, selected)
        self.assertNotIn(previous.name, selected)
        identity = runtime_admission.implementation()
        for task in (previous, current):
            relative = (task / 'environment/app/package.json').relative_to(root).as_posix()
            self.assertEqual(identity[relative], hashlib.sha256((root / relative).read_bytes()).hexdigest())

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
