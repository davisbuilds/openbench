"""Trusted grading controls; Docker tests are opt-in with a pinned local image."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from obench.sandbox_grading import (
    GradingError, CandidateFailure, bounded_command, comparison, dojo_cases,
    grade_dojo, grade_submission, freeze_submission, source_archive, task_digest, task_manifest, validate_task_binding,
)

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / 'harbor-tasks-local/dojo-evidence-pr60-v2/environment/app'
REFERENCE = REPO / 'tasks-local/dojo-evidence-pr60/solution'
IMAGE = os.environ.get('OBENCH_GRADING_TEST_IMAGE')


class ArtifactTests(unittest.TestCase):
    def test_regular_source_acceptance_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            p=root/'scripts/profiles/probe.py';p.parent.mkdir(parents=True)
            p.write_text('raise RuntimeError("must never import on host")')
            allowed={'scripts/profiles/probe.py'}
            archive, receipt=source_archive(root,allowed)
            self.assertGreater(len(archive),0)
            self.assertEqual(set(receipt),allowed)
            outside=root.parent/(root.name+'-outside')
            outside.write_text('private')
            try:
                p.unlink();p.symlink_to(outside)
                with self.assertRaises(GradingError):source_archive(root,allowed)
            finally:outside.unlink()

    def test_unexpected_missing_hardlink_and_oversize_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'a.py';p.write_text('x')
            with self.assertRaises(GradingError):source_archive(root,{'b.py'})
            os.link(p,root/'b.py')
            with self.assertRaises(GradingError):source_archive(root,{'a.py','b.py'})
            (root/'b.py').unlink();p.write_bytes(b'x'*(2*1024*1024+1))
            with self.assertRaises(GradingError):source_archive(root,{'a.py'})

    def test_worker_protocol_has_no_rewards_or_expectations(self):
        requests=[case[1] for case in dojo_cases()]
        self.assertTrue(requests)
        self.assertNotIn('expected',json.dumps(requests))
        self.assertNotIn('reward',json.dumps(requests))
        self.assertFalse(comparison({'reward':1},None))

    def test_bounded_output_timeout_and_success(self):
        self.assertEqual(bounded_command([sys.executable,'-c','print("ok")']),b'ok\n')
        with self.assertRaisesRegex(GradingError,'output limit'):
            bounded_command([sys.executable,'-c','import os; os.write(1,b"x"*1000000)'],limit=128)
        with self.assertRaisesRegex(GradingError,'timeout'):
            bounded_command([sys.executable,'-c','import time; time.sleep(5)'],timeout=.1)


class DiagnosticContractTests(unittest.TestCase):
    def diagnostic(self, **changes):
        return dict(kind='surface-mismatch', live_surface='exec',
                    recorded_surface='codex-tui', live_entries=1, recorded_entries=1,
                    only_in_live=[], only_in_recorded=[],
                    detail='source or locator changed for review', **changes)

    def test_name_only_diagnostics_are_valid_in_v4_but_not_legacy_v3(self):
        value = self.diagnostic()
        self.assertFalse(comparison(value, (1, 1, 1, 1)))
        self.assertTrue(comparison(value, (1, 1, 1, 1), oracle_version=4))
        qualified = dict(value, only_in_live=['dojo:review'],
                         only_in_recorded=['connector:review'])
        self.assertTrue(comparison(qualified, (1, 1, 1, 1), oracle_version=4))

    def test_representation_freedom_does_not_accept_missing_or_malformed_detection(self):
        value = self.diagnostic()
        for wrong in (None, {}, dict(value, kind='equal'),
                      dict(value, live_entries=0), dict(value, live_entries=True),
                      dict(value, only_in_live='review'),
                      dict(value, only_in_recorded=[{}]), dict(value, detail=None)):
            with self.subTest(wrong=wrong):
                self.assertFalse(comparison(wrong, (1, 1, 1, 1), oracle_version=4))
        self.assertFalse(comparison(value, None, oracle_version=4))
        self.assertTrue(comparison(None, None, oracle_version=4))


class FreezeClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_proven_stopped_candidate_rejection_scores_zero(self):
        from obench.harbor_sandbox import SandboxArtifactError, SandboxError
        class Environment:
            def __init__(self, failure): self.failure=failure
            async def freeze_source(self, _destination): raise self.failure
        proven={'solver_stopped':True,'broker_revoked':True}
        receipt, graded=await freeze_submission(Environment(SandboxArtifactError('symlink',proven)),Path('/unused'))
        self.assertEqual(receipt,proven)
        self.assertEqual(graded['score'],0)
        self.assertEqual(graded['candidate_failure'],'invalid_source_artifact')
        with self.assertRaisesRegex(GradingError,'unsealed'):
            await freeze_submission(Environment(SandboxArtifactError('symlink',{})),Path('/unused'))
        with self.assertRaises(SandboxError):
            await freeze_submission(Environment(SandboxError('Docker unavailable')),Path('/unused'))


class TaskBindingTests(unittest.TestCase):
    def make_task(self, root):
        root.mkdir()
        (root/'instruction.md').write_text('Repair the observable behavior.')
        (root/'task.toml').write_text(
            '[metadata.openbench_task_content_digest]\nscheme = 3\nsha256 = "' + '0'*64
            + '"\n[environment]\nnetwork_mode = "no-network"\n')
        source = root/'environment/app/scripts/profiles/a.py'
        source.parent.mkdir(parents=True)
        source.write_text('raise RuntimeError("candidate must never import on host")\n')
        (root/'environment/.dockerignore').write_text('**/.git\n')
        return source

    def seal(self, root):
        import re
        digest = task_digest(root)
        p = root/'task.toml'
        p.write_text(re.sub(r'sha256 = "[0-9a-f]{64}"', 'sha256 = "'+digest+'"', p.read_text()))
        expected = {'scheme':3, 'sha256':digest}
        validate_task_binding(root, expected)
        return expected

    def test_source_policy_and_hidden_build_inputs_are_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'task'
            source=self.make_task(root)
            for path in [source, root/'task.toml', root/'environment/.dockerignore']:
                expected=self.seal(root)
                old=path.read_text()
                path.write_text(old+'\n# changed trusted input\n')
                # TOML comments intentionally canonicalize away; mutate actual policy.
                if path.name=='task.toml':
                    path.write_text(old.replace('no-network','public-network'))
                with self.assertRaisesRegex(GradingError,'binding mismatch'):
                    validate_task_binding(root,expected)
                path.write_text(old)
            expected=self.seal(root)
            self.assertEqual(task_digest(root), expected['sha256'])
            self.assertEqual(task_manifest(root)['scheme'],3)

    def test_oracle_module_change_invalidates_unchanged_task(self):
        import obench.sandbox_grading as grading
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'task'
            self.make_task(root)
            expected=self.seal(root)
            cloned=parent/'trusted_grader_copy.py'
            cloned.write_bytes(Path(grading.__file__).read_bytes())
            command = [sys.executable,'-B','-c',
                'import importlib.util,sys; from pathlib import Path; '
                'spec=importlib.util.spec_from_file_location("trusted_copy",sys.argv[1]); '
                'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); '
                'print(module.task_digest(Path(sys.argv[2])))',str(cloned),str(root)]
            same=subprocess.check_output(command,text=True).strip()
            self.assertEqual(same,expected['sha256'])
            cloned.write_text(cloned.read_text()+'\n# New trusted oracle revision\n')
            changed=subprocess.check_output(command,text=True).strip()
            self.assertNotEqual(changed,expected['sha256'])

    def test_module_edited_after_import_cannot_claim_new_digest(self):
        import obench.sandbox_grading as grading
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'task'
            self.make_task(root)
            cloned=parent/'trusted_grader_copy.py'
            cloned.write_bytes(Path(grading.__file__).read_bytes())
            command = [sys.executable,'-B','-c',
                'import importlib.util,sys; from pathlib import Path; '
                'p=Path(sys.argv[1]); spec=importlib.util.spec_from_file_location("trusted_copy",p); '
                'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); '
                'p.write_text(p.read_text()+"\\n# changed after import\\n"); '
                'module.task_digest(Path(sys.argv[2]))',str(cloned),str(root)]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('trusted grader changed after import',result.stderr)

    def test_scheme2_and_linked_task_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'task';source=self.make_task(root)
            expected=self.seal(root)
            with self.assertRaisesRegex(GradingError,'scheme 3'):
                validate_task_binding(root,{'scheme':2,'sha256':expected['sha256']})
            source.unlink();source.symlink_to(root/'instruction.md')
            with self.assertRaisesRegex(GradingError,'artifact type'):
                task_digest(root)


@unittest.skipUnless(IMAGE and BASE.is_dir(), 'set OBENCH_GRADING_TEST_IMAGE to a pinned local image ID')
class DockerGradingTests(unittest.TestCase):
    def prepare(self,root,parts=()):
        allowed={p.relative_to(BASE).as_posix() for p in (BASE/'scripts/profiles').glob('*.py')}
        for relative in allowed:
            p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(BASE/relative,p)
        for name in parts:
            shutil.copy2(REFERENCE/'scripts/profiles'/name,root/'scripts/profiles'/name)
        return allowed

    def test_buggy_reference_partial_and_valid_alternative(self):
        controls=[('buggy',(),0),('budget',('budget.py',),.3333),
                  ('rollout-mismatch',('rollout_codex.py',),.6667),
                  ('reference',('budget.py','rollout_codex.py'),1),
                  ('alternative',('budget.py','rollout_codex.py'),1)]
        for name,parts,score in controls:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);allowed=self.prepare(root,parts)
                if name=='alternative':
                    p=root/'scripts/profiles/rollout_codex.py';s=p.read_text()
                    s=s.replace('sorted(only_recorded.elements())','[s.split(":",1)[1] for s in sorted(only_recorded.elements())]')
                    s=s.replace('sorted(only_live.elements())','[s.split(":",1)[1] for s in sorted(only_live.elements())]')
                    p.write_text(s)
                graded=grade_dojo(root,allowed,IMAGE)
                self.assertEqual(graded['score'],score)
                self.assertFalse(graded['worker']['host_mounts'])

    def test_v4_accepts_valid_representations_and_rejects_partial_detection(self):
        controls = [('reference', 1), ('name-only', 1), ('unqualified', 1),
                    ('always-mismatch', .6667), ('never-mismatch', .6667),
                    ('names-only-detection', .6667), ('wrong-counts', .6667)]
        for name, score in controls:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                allowed = self.prepare(root, ('budget.py', 'rollout_codex.py'))
                path = root / 'scripts/profiles/rollout_codex.py'
                text = path.read_text()
                if name in ('name-only', 'unqualified'):
                    for field in ('live', 'recorded'):
                        old = f'sorted(only_{field}.elements())'
                        new = ('[]' if name == 'name-only' else
                               f'[s.split(":", 1)[1] for s in sorted(only_{field}.elements())]')
                        self.assertIn(old, text)
                        text = text.replace(old, new)
                elif name == 'always-mismatch':
                    self.assertIn('if live_ids == recorded_ids:', text)
                    text = text.replace('if live_ids == recorded_ids:', 'if False:')
                elif name == 'never-mismatch':
                    text += '\ndef surface_mismatch(live, recorded): return None\n'
                elif name == 'names-only-detection':
                    text = text.replace('if live_ids == recorded_ids:',
                        'if {e.name for e in live.entries} == {e.name for e in recorded.listing.entries}:')
                elif name == 'wrong-counts':
                    text = text.replace('sum(live_ids.values())', 'len(live_ids)')
                path.write_text(text)
                graded = grade_dojo(root, allowed, IMAGE, oracle_version=4)
                self.assertEqual(graded['score'], score)
                self.assertEqual(graded['oracle_version'], 4)

    def test_candidate_cannot_write_trusted_reward_or_read_host_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);root=base/'source';root.mkdir()
            allowed=self.prepare(root,('budget.py','rollout_codex.py'))
            oracle=base/'oracle-secret';oracle.write_text('trusted-only-secret')
            reward=base/'reward.txt';reward.write_text('parent-owned')
            target=root/'scripts/profiles/__init__.py'
            child_code = ('import time\ntry:\n open('+repr(str(oracle))+').read()\n print("exposed",flush=True)\nexcept OSError:\n print("blocked",flush=True)\nwhile True:\n try:\n  open('+repr(str(reward))+',"w").write("1.0")\n except OSError:\n  pass\n time.sleep(.01)')
            target.write_text(target.read_text()+f'''
import os as _os, subprocess as _subprocess, sys as _sys
try:
    _secret = open({str(oracle)!r}).read()
    print('ORACLE_EXPOSED:' + _secret)
except OSError:
    pass
for _path in [{str(reward)!r}, '/logs/verifier/reward.txt', '/tests/oracle.py']:
    try:
        with open(_path, 'w') as _out: _out.write('1.0')
        print('WRITE_EXPOSED:' + _path)
    except OSError:
        pass
# A candidate child remains confined to this throwaway worker; no later secret
# or authoritative output directory is ever introduced into its namespace.
_child_code = {child_code!r}
_child = _subprocess.Popen([_sys.executable,'-c',_child_code],stdout=_subprocess.PIPE,stderr=_subprocess.DEVNULL,text=True)
assert _child.stdout.readline().strip() == 'blocked'
''')
            self.assertEqual(grade_dojo(root,allowed,IMAGE)['score'],1)
            self.assertEqual(oracle.read_text(),'trusted-only-secret')
            self.assertEqual(reward.read_text(),'parent-owned')

    def test_invalid_submission_zero_but_missing_image_is_infrastructure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);allowed=self.prepare(root)
            p=root/'scripts/profiles/__init__.py'
            original=p.read_bytes()
            p.unlink();p.symlink_to(root/'scripts/profiles/budget.py')
            graded=grade_submission(root,allowed,IMAGE)
            self.assertEqual(graded['score'],0)
            self.assertIn('artifact',graded['candidate_failure'])
            p.unlink();p.write_bytes(original)
            with self.assertRaises(GradingError) as raised:
                grade_submission(root,allowed,'sha256:'+'0'*64)
            self.assertNotIsInstance(raised.exception,CandidateFailure)

    def test_malformed_flood_and_timeout_cannot_supply_reward(self):
        for code,reason in [('print("{bad}"); raise SystemExit(0)','malformed'),
                            ('raise RuntimeError("candidate import failure")','worker command failed'),
                            ('print("x"*1000000); raise SystemExit(0)','output limit'),
                            ('import time; time.sleep(60)','timeout')]:
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);allowed=self.prepare(root)
                (root/'scripts/profiles/__init__.py').write_text(code)
                with self.assertRaisesRegex(CandidateFailure,reason):
                    grade_dojo(root,allowed,IMAGE,timeout=2)
                graded = grade_submission(root,allowed,IMAGE,timeout=2)
                self.assertEqual(graded['score'],0)
                self.assertIn(reason,graded['candidate_failure'])


if __name__=='__main__':unittest.main()
