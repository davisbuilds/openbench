"""Checkout organization must not change installed-project discovery."""
from pathlib import Path
import tempfile
import unittest
from obench import paths


class RepositoryLayoutTests(unittest.TestCase):
    def test_grouped_checkout_discovers_all_tiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('core','local','imported'):
                (root/'benchmarks'/name).mkdir(parents=True)
            self.assertEqual(paths.default_tasks_dir(directory),str(root/'benchmarks/core'))
            self.assertEqual(paths.default_local_tasks_dir(directory),str(root/'benchmarks/local'))
            self.assertEqual(paths.default_imported_tasks_dir(directory),str(root/'benchmarks/imported'))

    def test_existing_custom_project_tiers_still_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('tasks','tasks-local','tasks-imported'):
                (root/name).mkdir()
            self.assertEqual(paths.default_tasks_dir(directory),str(root/'tasks'))
            self.assertEqual(paths.default_local_tasks_dir(directory),str(root/'tasks-local'))
            self.assertEqual(paths.default_imported_tasks_dir(directory),str(root/'tasks-imported'))

    def test_configured_private_project_keeps_its_task_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.openbench/tasks').mkdir(parents=True)
            self.assertEqual(paths.default_tasks_dir(directory),str(root/'.openbench/tasks'))
            self.assertIsNone(paths.default_local_tasks_dir(directory))
            self.assertIsNone(paths.default_imported_tasks_dir(directory))
