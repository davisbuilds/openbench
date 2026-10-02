import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from obench.frozen_context import freeze_context, load_archive, main
from obench.context_drift import audit_skills, parse_roots


class ContextDriftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.capture = self.root / 'capture'
        skill = self.capture / 'codex/skills/example'
        (skill / 'scripts').mkdir(parents=True)
        (skill / 'SKILL.md').write_text('Use the helper.\n')
        (skill / 'scripts/check.sh').write_text('#!/bin/sh\nexit 0\n')
        (skill / 'scripts/check.sh').chmod(0o755)
        (self.capture / 'codex/AGENTS.md').write_text('Run tests.\n')
        self.archive = self.root / 'context.tar'
        self.digest = freeze_context(self.capture, self.archive)
        self.loaded = load_archive(self.archive, self.digest, kind='context')
        self.canonical = self.root / 'canonical'
        shutil.copytree(skill, self.canonical / 'example')
        self.global_root = self.root / 'global'
        self.global_root.mkdir()
        (self.global_root / 'example').symlink_to(self.canonical / 'example', target_is_directory=True)

    def audit(self, provenance=None):
        return audit_skills(self.loaded, {'dojo': self.canonical, 'global': self.global_root}, provenance)

    def test_equal_sources_and_installed_root_symlink_are_clean(self):
        report = self.audit()
        self.assertEqual(report['status'], 'clean')
        self.assertEqual(report['skills'], ['example'])
        self.assertEqual(len(report['sources']), 2)

    def test_full_tree_content_add_remove_and_mode_changes_are_visible(self):
        skill = self.canonical / 'example'
        (skill / 'SKILL.md').write_text('New guidance.\n')
        (skill / 'scripts/check.sh').chmod(0o644)
        (skill / 'references').mkdir()
        (skill / 'references/new.md').write_text('New reference.\n')
        report = self.audit()
        self.assertEqual(report['status'], 'drift')
        changes = report['sources'][0]['skills']['example']['changes']
        self.assertEqual({(c['path'], c['kind']) for c in changes}, {
            ('SKILL.md', 'content'), ('scripts/check.sh', 'mode'), ('references/new.md', 'added')})
        (skill / 'SKILL.md').unlink()
        self.assertIn({'path': 'SKILL.md', 'kind': 'removed'}, self.audit()['sources'][0]['skills']['example']['changes'])
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.digest)

    def test_missing_root_is_incomplete_not_clean(self):
        report = audit_skills(self.loaded, {'absent': self.root / 'absent'})
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['sources'][0]['skills']['example']['status'], 'unavailable')

    def test_nested_symlink_and_special_file_do_not_get_followed(self):
        child = self.canonical / 'example/extra'
        child.symlink_to(self.root / 'absent')
        self.assertEqual(self.audit()['status'], 'incomplete')
        child.unlink()
        import os
        os.mkfifo(child)
        self.assertEqual(self.audit()['status'], 'incomplete')

    def provenance(self):
        return {'files': [{'destination': name, 'original_sha256': info['sha256'],
                           'staged_sha256': info['sha256']}
                          for name, info in self.loaded.manifest['files'].items()
                          if name.startswith('codex/skills/')]}

    def test_adaptation_uses_recorded_original_then_detects_later_drift(self):
        provenance = self.provenance()
        original = b'Host paths before adaptation.\n'
        (self.canonical / 'example/SKILL.md').write_bytes(original)
        entry = next(e for e in provenance['files'] if e['destination'].endswith('/SKILL.md'))
        entry['original_sha256'] = hashlib.sha256(original).hexdigest()
        report = self.audit(provenance)
        self.assertEqual(report['status'], 'clean')
        self.assertEqual(report['adapted_files'], ['codex/skills/example/SKILL.md'])
        (self.canonical / 'example/SKILL.md').write_text('Changed after capture.\n')
        self.assertEqual(self.audit(provenance)['status'], 'drift')

    def test_unbound_duplicate_or_incomplete_provenance_rejected(self):
        for change in ('tamper', 'duplicate', 'missing'):
            p = self.provenance()
            if change == 'tamper':
                p['files'][0]['staged_sha256'] = '0' * 64
            elif change == 'duplicate':
                p['files'].append(p['files'][0])
            else:
                p['files'].pop()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.audit(p)

    def test_no_skills_cannot_report_clean(self):
        shutil.rmtree(self.capture / 'codex/skills')
        path = self.root / 'empty-skills.tar'
        loaded = load_archive(path, freeze_context(self.capture, path), kind='context')
        with self.assertRaisesRegex(ValueError, 'skills'):
            audit_skills(loaded, {'dojo': self.canonical})

    def test_unselected_skills_and_disposable_files_are_out_of_scope(self):
        (self.canonical / 'unselected').mkdir()
        (self.canonical / 'unselected/SKILL.md').write_text('Not in treatment.')
        (self.canonical / 'example/__pycache__').mkdir()
        (self.canonical / 'example/__pycache__/generated.pyc').write_bytes(b'cache')
        self.assertEqual(self.audit()['status'], 'clean')

    def test_duplicate_labels_cannot_silently_drop_a_source(self):
        with self.assertRaises(ValueError):
            parse_roots(['global=/first', 'global=/second'])

    def test_corrupt_archive_is_rejected_before_a_freshness_claim(self):
        self.archive.write_bytes(b'corrupt')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['audit-skills', str(self.archive), '--sha256', self.digest,
                  '--skills-root', 'dojo=' + str(self.canonical)])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_json_warning_and_distinct_exit_codes(self):
        args = ['audit-skills', str(self.archive), '--sha256', self.digest,
                '--skills-root', 'dojo=' + str(self.canonical)]
        for expected in (0, 1, 2):
            if expected == 1:
                (self.canonical / 'example/SKILL.md').write_text('Changed.\n')
            elif expected == 2:
                shutil.rmtree(self.canonical)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(args), expected)
            self.assertEqual(json.loads(out.getvalue())['status'], ('clean', 'drift', 'incomplete')[expected])
            if expected:
                self.assertIn('WARNING', err.getvalue())
