import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from obench.frozen_context import freeze_context, load_archive, freeze_checkout, materialize, verify_session_context


class FrozenContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        (self.source / 'codex/skills/example/scripts').mkdir(parents=True)
        (self.source / 'codex/AGENTS.md').write_text('Run relevant tests.\n')
        (self.source / 'codex/skills/example/SKILL.md').write_text('Use scripts/check.sh\n')
        script = self.source / 'codex/skills/example/scripts/check.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o755)

    def test_snapshot_is_deterministic_and_does_not_follow_later_edits(self):
        first, second = self.root / 'first.tar', self.root / 'second.tar'
        seal = freeze_context(self.source, first)
        self.assertEqual(seal, freeze_context(self.source, second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        (self.source / 'codex/AGENTS.md').write_text('changed')
        archive = load_archive(first, seal, kind='context')
        materialize(archive, self.root / 'staged')
        self.assertEqual((self.root / 'staged/codex/AGENTS.md').read_text(), 'Run relevant tests.\n')
        self.assertEqual((self.root / 'staged/codex/skills/example/scripts/check.sh').stat().st_mode & 0o777, 0o755)
        with self.assertRaises(ValueError):
            materialize(archive, self.root / 'staged')

    def test_rejects_auth_config_history_and_source_links(self):
        for name in ('codex/auth.json', 'codex/config.toml', 'codex/sessions/history.jsonl', '.git/config'):
            p = self.source / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('forbidden')
            with self.subTest(name=name), self.assertRaises(ValueError):
                freeze_context(self.source, self.root / 'bad.tar')
            p.unlink()
        link = self.source / 'resources/outside'
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(self.root / 'outside')
        with self.assertRaises(ValueError):
            freeze_context(self.source, self.root / 'bad.tar')

    def test_tamper_and_wrong_kind_rejected(self):
        p = self.root / 'bundle.tar'
        seal = freeze_context(self.source, p)
        with self.assertRaises(ValueError):
            load_archive(p, '0' * 64, kind='context')
        with self.assertRaises(ValueError):
            load_archive(p, seal, kind='checkout')
        with p.open('ab') as f:
            f.write(b'tamper')
        with self.assertRaises(ValueError):
            load_archive(p, seal, kind='context')

    def test_special_inputs_do_not_block_and_hardlinks_are_rejected(self):
        fifo = self.source / 'resources/fifo'
        fifo.parent.mkdir()
        os.mkfifo(fifo)
        # Separate process bounds the regression: blocking open used to wait
        # forever before checking that this was not a regular file.
        import sys
        result = subprocess.run([sys.executable, '-m', 'obench.frozen_context',
                                 'context', str(self.source), '--output', str(self.root/'bad.tar')],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        fifo.unlink()
        os.link(self.source/'codex/AGENTS.md', fifo)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            freeze_context(self.source, self.root/'bad.tar')

    def test_malicious_archive_members_rejected_even_with_matching_digest(self):
        for name, member_type in [('../outside', tarfile.REGTYPE), ('/outside', tarfile.REGTYPE),
                                  ('payload/codex/AGENTS.md', tarfile.SYMTYPE)]:
            p = self.root / 'attack.tar'
            with tarfile.open(p, 'w') as tar:
                info = tarfile.TarInfo(name); info.type = member_type
                if member_type == tarfile.SYMTYPE:
                    info.linkname = '/outside'
                tar.addfile(info)
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_archive(p, hashlib.sha256(p.read_bytes()).hexdigest(), kind='context')
        self.assertFalse((self.root / 'outside').exists())

    def test_checkout_uses_pinned_git_objects_not_worktree(self):
        repo = self.root / 'repo'; repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q'); git('config', 'user.name', 'test'); git('config', 'user.email', 'test@example.invalid')
        (repo / 'source.py').write_text('broken\n')
        (repo / 'AGENTS.md').write_text('Run tests.\n')
        (repo / 'answers.md').write_text('answer\n')
        git('add', 'source.py', 'AGENTS.md', 'answers.md');git('commit', '-qm', 'before')
        commit = git('rev-parse', 'HEAD')
        (repo / 'source.py').write_text('fixed\n')
        (repo / 'private.txt').write_text('private\n')
        archive = self.root / 'checkout.tar'
        seal = freeze_checkout(repo, commit, archive, exclusions={'answers.md': 'Historical solution notes'})
        loaded = load_archive(archive, seal, kind='checkout')
        materialize(loaded, self.root / 'checkout')
        self.assertEqual((self.root / 'checkout/source.py').read_text(), 'broken\n')
        self.assertEqual(sorted(p.name for p in (self.root / 'checkout').iterdir()), ['AGENTS.md', 'source.py'])
        with self.assertRaises(ValueError):
            freeze_checkout(repo, 'HEAD', self.root / 'unpinned.tar')

    def test_actual_initial_instructions_required_not_assistant_claims(self):
        archive = self.root / 'context.tar'
        loaded = load_archive(archive, freeze_context(self.source, archive), kind='context')
        sessions = self.root / 'sessions'; sessions.mkdir()
        session = sessions / 'run.jsonl'
        def message(role, text):
            return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                    'content': [{'type': 'input_text', 'text': text}]}}
        evidence = '# AGENTS.md instructions for /app\nRun relevant tests.\n/tmp/codex-home/skills/example/SKILL.md'
        catalog = '<skills_instructions>\n## Skills\n### Skill roots\n- `r0` = `/tmp/codex-home/skills`\n### Available skills\n- example: Example workflow. (file: r0/example/SKILL.md)\n</skills_instructions>'
        def write(*messages):
            session.write_text(''.join(json.dumps(m)+'\n' for m in messages))
        write(message('developer', catalog), message('user', evidence))
        self.assertEqual(verify_session_context(sessions, loaded)['skills_discovered'], ['example'])
        write(message('assistant', evidence))
        with self.assertRaisesRegex(ValueError, 'global guidance'):
            verify_session_context(sessions, loaded)
        # A quoted path or even a quoted catalog in user guidance is not the
        # harness-generated catalog. Wrong roots and later echoes also fail.
        for messages in (
            [message('user', evidence)],
            [message('user', evidence + '\n' + catalog)],
            [message('developer', catalog.replace('/tmp/codex-home/skills', '/other/skills')), message('user', evidence)],
            [message('user', evidence), message('assistant', 'done'), message('developer', catalog)],
        ):
            write(*messages)
            with self.subTest(messages=messages), self.assertRaisesRegex(ValueError, 'skills'):
                verify_session_context(sessions, loaded)


if __name__ == '__main__':
    unittest.main()
