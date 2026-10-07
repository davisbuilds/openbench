"""Real file changes distinguish workflow, runtime and reporting identities."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from obench import runtime_admission as ra, repair_validation as rv, repair_workflow as rw
from obench.evidence_identity import runtime_sources


class EvidenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ra.ROOT/'obench', self.root/'obench', ignore=shutil.ignore_patterns('tests', '__pycache__'))
        for relative in (*ra.SCRIPTS, 'docker/repair-sandbox/Dockerfile', 'obench/tests/test_sandbox_gateway.py'):
            path = self.root/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ra.ROOT/relative, path)
        self.root_patch = patch.object(ra, 'ROOT', self.root)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)

    def change(self, relative):
        with (self.root/relative).open('a') as stream:
            stream.write('\n# changed bytes\n')

    def test_report_only_edit_keeps_runtime_but_execution_edit_invalidates(self):
        before = ra.implementation()
        self.change('obench/report.py')
        self.change('obench/results_query.py')
        self.assertEqual(ra.implementation(), before)
        for relative in ('obench/sandbox_gateway.py', 'obench/harbor_sandbox.py',
                         'obench/repair_worker.py', 'obench/frozen_context.py',
                         'obench/harbor_agents/sandbox_codex.py', 'obench/evidence_identity.py'):
            current = ra.implementation()
            self.change(relative)
            self.assertNotEqual(ra.implementation(), current, relative)

    def test_dynamic_oracles_and_adapters_remain_bound(self):
        hashes = ra.implementation()
        for relative in ('obench/repair_oracles/agentmonitor.py', 'obench/repair_oracles/dojo_v6.py',
                         'obench/adapters/codex.py', 'obench/harbor_agents/codex.py'):
            self.assertIn(relative, hashes)
            self.change(relative)
            self.assertNotEqual(ra.implementation()[relative], hashes[relative])

    def test_new_transitive_and_script_imports_automatically_join_identity(self):
        helper = self.root/'obench/new_runtime_helper.py'
        helper.write_text('from . import report\n')
        before = ra.implementation()
        self.assertNotIn('obench/new_runtime_helper.py', before)
        with (self.root/ra.SCRIPTS[0]).open('a') as stream:
            stream.write('\nfrom obench import new_runtime_helper\n')
        after = ra.implementation()
        self.assertIn('obench/new_runtime_helper.py', after)
        self.assertIn('obench/report.py', after)
        self.change('obench/new_runtime_helper.py')
        self.assertNotEqual(ra.implementation(), after)

    def test_package_initializers_and_literal_dynamic_names_are_bound(self):
        folder = self.root/'obench/new_package'
        folder.mkdir()
        (folder/'__init__.py').write_text('')
        (folder/'worker.py').write_text('from .. import report\n')
        with (self.root/'obench/sandbox_gateway.py').open('a') as stream:
            stream.write("\nDYNAMIC_WORKER = 'obench.new_package.worker:Worker'\n")
        after = ra.implementation()
        for relative in ('obench/new_package/__init__.py', 'obench/new_package/worker.py', 'obench/report.py'):
            self.assertIn(relative, after)

    def test_missing_runtime_root_is_an_error(self):
        (self.root/'obench/sandbox_gateway.py').unlink()
        with self.assertRaisesRegex(ValueError, 'entry point'):
            runtime_sources(self.root, ra.SCRIPTS)

    def test_quality_change_does_not_invalidate_workflow_contract(self):
        workflow_before = rw.identity()
        with patch.object(rv, 'ROOT', self.root):
            self.change('obench/repair_validation.py')
            with self.assertRaisesRegex(ValueError, 'changed after import'):
                rv.implementation()
        self.assertEqual(rw.identity(), workflow_before)
        copied = self.root/'obench/repair_workflow.py'
        with patch.object(rw, '__file__', str(copied)):
            self.assertEqual(rw.identity(), workflow_before)
            self.change('obench/repair_workflow.py')
            with self.assertRaisesRegex(ValueError, 'changed after import'):
                rw.identity()
