"""Case and scoring revisions are separate; legacy names never change meaning."""
import copy
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
import unittest

from obench import init, repair_identity, stats, suite_run
from obench.sandbox_grading import GradingError, dojo_oracle_version, task_manifest, task_digest
from obench.repair_oracles import registry


class RevisionSelectionTests(unittest.TestCase):
    def dojo(self, name='dojo-evidence-pr60-c2-o5', oracle_revision=5):
        return {'openbench_task': name, 'openbench_revision': {
            'case': 'dojo-evidence-pr60', 'case_revision': 2,
            'oracle': 'dojo-evidence', 'oracle_revision': oracle_revision,
        }}

    def test_explicit_oracle_revision_selects_without_flat_task_counter(self):
        for revision in (4, 5):
            metadata = self.dojo(f'dojo-evidence-pr60-c2-o{revision}', revision)
            self.assertEqual(dojo_oracle_version(metadata), revision)

    def test_legacy_alias_cannot_be_reinterpreted_by_explicit_metadata(self):
        metadata = self.dojo('dojo-evidence-pr60-v5', 4)
        with self.assertRaisesRegex(GradingError, 'revision'):
            dojo_oracle_version(metadata)

    def test_registered_oracle_accepts_independent_case_revision(self):
        metadata = {'openbench_task': 'am-benchmark-pr106-c1-o3',
                    'openbench_oracle': 'agentmonitor-benchmark-v3',
                    'openbench_revision': {
                        'case': 'am-benchmark-pr106', 'case_revision': 1,
                        'oracle': 'agentmonitor-benchmark', 'oracle_revision': 3}}
        self.assertEqual(registry.select(metadata).id, 'agentmonitor-benchmark-v3')

    def test_malformed_or_cross_case_revisions_cannot_select_an_oracle(self):
        for field, invalid in [('case_revision', True), ('case_revision', 0),
                               ('case_revision', '2'), ('oracle_revision', 99),
                               ('oracle', 'agentmonitor-benchmark'), ('case', [])]:
            metadata = self.dojo()
            metadata['openbench_revision'][field] = invalid
            with self.subTest(field=field, value=invalid), self.assertRaises(GradingError):
                dojo_oracle_version(metadata)
        for declaration in (None, {}, [], {**self.dojo()['openbench_revision'], 'extra': 1}):
            metadata = self.dojo()
            metadata['openbench_revision'] = declaration
            with self.subTest(declaration=declaration), self.assertRaises(GradingError):
                dojo_oracle_version(metadata)
        metadata = self.dojo()
        del metadata['openbench_revision']
        with self.assertRaises(GradingError):
            dojo_oracle_version(metadata)
        metadata = self.dojo()
        metadata['openbench_oracle'] = 'agentmonitor-benchmark-v3'
        with self.assertRaises(GradingError):
            registry.select(metadata)


ROOT = Path(__file__).resolve().parents[2] / 'benchmarks/harbor/local'


class RevisionManifestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        init.init_scaffold(self.root)
        self.suite = self.root / '.openbench/suites/default.toml'
        self.suite.write_text(self.suite.read_text().replace('gpt-5.6-sol', 'gpt-6-sol-high')
                              + '\n[sandbox]\nkind = "repair-v1"\nruntime_image = "sha256:'
                              + 'a' * 64 + '"\n')
        self.tasks = self.root / '.openbench/tasks'
        shutil.rmtree(self.tasks)

    def stage(self, old, new=None, oracle_revision=None):
        if self.tasks.exists():
            shutil.rmtree(self.tasks)
        target = self.tasks / (new or old)
        shutil.copytree(ROOT / old, target)
        config = target / 'task.toml'
        text = config.read_text().replace(old, new or old)
        if oracle_revision is not None:
            text = text.replace('oracle_revision = 5', f'oracle_revision = {oracle_revision}')
        config.write_text(text)
        self.reseal(target)
        return target

    def reseal(self, target):
        config = target / 'task.toml'
        metadata = tomllib.loads(config.read_text())['metadata']
        digest = (registry.task_digest if metadata.get('openbench_oracle') else task_digest)(target)
        config.write_text(re.sub(r'(scheme = [34]\nsha256 = ")[a-f0-9]{64}',
                                lambda match: match[1] + digest, config.read_text()))

    def validate(self, manifest):
        digest = hashlib.sha256(stats._canonical_suite_manifest_bytes(manifest)).hexdigest()
        stats._validate_suite_manifest_shape(manifest, digest)

    def test_oracle_only_change_preserves_case_fingerprint_but_changes_suite_seal(self):
        runs = []
        jobs = []
        for oracle in (4, 5):
            task = self.stage('dojo-evidence-pr60-v5', f'dojo-evidence-pr60-c2-o{oracle}', oracle)
            run = suite_run.compile_suite(self.suite)
            self.validate(run.manifest)
            revision = run.manifest['task_sets'][0]['repair_revision']
            self.assertEqual((revision['case_revision'], revision['oracle_revision']), (2, oracle))
            self.assertEqual(dojo_oracle_version(tomllib.loads((task / 'task.toml').read_text())['metadata']), oracle)
            self.assertIn('obench.repair_identity', run.manifest['sandbox']['implementation_sha256'])
            runs.append(run)
            jobs.append(suite_run.plan_jobs(run)[0].artifact.sha256)
        self.assertEqual(runs[0].manifest['task_sets'][0]['repair_revision']['case_sha256'],
                         runs[1].manifest['task_sets'][0]['repair_revision']['case_sha256'])
        self.assertNotEqual(runs[0].manifest_sha256, runs[1].manifest_sha256)
        self.assertNotEqual(jobs[0], jobs[1])

    def test_registered_task_compiles_with_separate_revisions(self):
        self.stage('am-benchmark-pr106-v4', 'am-benchmark-pr106-c1-o3')
        run = suite_run.compile_suite(self.suite)
        self.validate(run.manifest)
        self.assertEqual(run.manifest['task_sets'][0]['repair_revision']['case_revision'], 1)
        job = suite_run.plan_jobs(run)[0].artifact.as_dict()
        self.assertEqual(job['verifier']['kwargs']['oracle_id'], 'agentmonitor-benchmark-v3')
        self.assertEqual(job['environment']['kwargs']['oracle_id'], 'agentmonitor-benchmark-v3')

    def test_execution_policy_change_does_not_advance_case_or_oracle(self):
        self.stage('dojo-evidence-pr60-v5')
        before = suite_run.compile_suite(self.suite)
        self.suite.write_text(self.suite.read_text().replace('gpt-6-sol-high', 'gpt-6-luna-max'))
        after = suite_run.compile_suite(self.suite)
        self.assertEqual(before.manifest['task_sets'][0]['repair_revision'],
                         after.manifest['task_sets'][0]['repair_revision'])
        self.assertNotEqual(before.manifest_sha256, after.manifest_sha256)

    def test_case_fingerprint_tracks_prompt_and_environment_not_oracle_or_docs(self):
        target = self.stage('dojo-evidence-pr60-v5')
        original = repair_identity.record(task_manifest(target))['case_sha256']
        original_full = task_digest(target)
        for name in ('instruction.md', 'environment/app/requirements.txt',
                     'environment/Dockerfile', 'README.md', 'tests/test.sh'):
            path = target / name
            old = path.read_bytes()
            path.write_bytes(old + b'\n# changed\n')
            actual = repair_identity.record(task_manifest(target))['case_sha256']
            with self.subTest(name=name):
                self.assertEqual(actual == original, name in ('README.md', 'tests/test.sh'))
                self.assertNotEqual(task_digest(target), original_full)
            path.write_bytes(old)

    def test_legacy_alias_lineage_matches_actual_case_inputs(self):
        records = {name: repair_identity.record(task_manifest(ROOT / name))
                   for name in repair_identity.LEGACY}
        for a, b in [('dojo-evidence-pr60-v4', 'dojo-evidence-pr60-v5'),
                     ('am-benchmark-pr106-v3', 'am-benchmark-pr106-v4')]:
            self.assertEqual(records[a]['case_revision'], records[b]['case_revision'])
            self.assertEqual(records[a]['case_sha256'], records[b]['case_sha256'])
            self.assertNotEqual(records[a]['oracle_revision'], records[b]['oracle_revision'])
        self.assertNotEqual(records['dojo-evidence-pr60-v3']['case_sha256'],
                            records['dojo-evidence-pr60-v4']['case_sha256'])
        self.assertEqual(records['dojo-evidence-pr60-v3']['case_revision'], 1)
        self.assertEqual(records['dojo-evidence-pr60-v4']['case_revision'], 2)

    def test_case_and_task_seals_bind_permissions_and_empty_build_directories(self):
        target = self.stage('dojo-evidence-pr60-v5')
        def identities():
            return repair_identity.record(task_manifest(target))['case_sha256'], task_digest(target)
        before = identities()
        script = target / 'environment/app/scripts/profiles/evidence.py'
        old_mode = script.stat().st_mode & 0o7777
        script.chmod(old_mode ^ 0o111)
        after = identities()
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        script.chmod(old_mode)
        self.assertEqual(identities(), before)
        empty = target / 'environment/app/empty-build-directory'
        empty.mkdir(mode=0o755)
        added = identities()
        self.assertNotEqual(before[0], added[0])
        self.assertNotEqual(before[1], added[1])
        empty.chmod(0o700)
        changed = identities()
        self.assertNotEqual(added[0], changed[0])
        self.assertNotEqual(added[1], changed[1])
        empty.rmdir()
        self.assertEqual(identities(), before)

    def test_historical_manifest_remains_readable_without_revision_records(self):
        self.stage('dojo-evidence-pr60-v5')
        historical = copy.deepcopy(suite_run.compile_suite(self.suite).manifest)
        del historical['sandbox']['implementation_sha256']['obench.repair_identity']
        del historical['sandbox']['implementation_sha256']['obench.repair_devtools']
        del historical['task_sets'][0]['repair_revision']
        self.validate(historical)

    def test_resealed_manifest_cannot_drop_or_mislabel_revision(self):
        self.stage('dojo-evidence-pr60-v5')
        manifest = suite_run.compile_suite(self.suite).manifest
        self.validate(manifest)
        for field, value in [('oracle_revision', 4), ('case_revision', True), ('case_sha256', 'invalid')]:
            changed = copy.deepcopy(manifest)
            changed['task_sets'][0]['repair_revision'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'revision'):
                self.validate(changed)
        missing = copy.deepcopy(manifest)
        del missing['task_sets'][0]['repair_revision']
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.validate(missing)


if __name__ == '__main__':
    unittest.main()
