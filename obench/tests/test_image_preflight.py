#!/usr/bin/env python3
"""A spec must not launch on a host lacking its pinned task images.

A live re-run was launched on a host holding 23 ``openbench-tb2`` images but
none at the digests its tasks pinned. Every cell died instantly with "cannot
inspect Docker image ...d7f5e336", the arm was stopped as a config error, and
0/63 cells ran. The existing preflight (``obench.bump_clis`` / version_drift)
checks CLI pins only, so nothing caught it before the launch.
"""

import os
import tempfile
import unittest

from obench import matrix_queue as mq


class ImagePreflightTests(unittest.TestCase):
    def _spec(self, image):
        root = tempfile.mkdtemp()
        task_dir = os.path.join(root, "tasks", "demo")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "task.toml"), "w", encoding="utf-8") as fh:
            fh.write(f'docker_image = "{image}"\n')
        spec = {"task_group": [{"tasks_dir": os.path.join(root, "tasks"),
                                "tasks": ["demo"]}]}
        return spec, root

    def test_absent_image_is_reported(self):
        spec, root = self._spec("repo@sha256:deadbeef")
        missing = mq.missing_task_images(spec, root, docker_runner=lambda ref: False)
        self.assertEqual(missing, [("demo", "repo@sha256:deadbeef")])

    def test_present_image_passes(self):
        spec, root = self._spec("repo@sha256:deadbeef")
        self.assertEqual(
            mq.missing_task_images(spec, root, docker_runner=lambda ref: True), [])

    def test_task_without_a_pinned_image_is_ignored(self):
        root = tempfile.mkdtemp()
        task_dir = os.path.join(root, "tasks", "demo")
        os.makedirs(task_dir)
        open(os.path.join(task_dir, "task.toml"), "w").close()
        spec = {"task_group": [{"tasks_dir": os.path.join(root, "tasks"),
                                "tasks": ["demo"]}]}
        self.assertEqual(
            mq.missing_task_images(spec, root, docker_runner=lambda ref: False), [])

    def test_the_exact_digest_matters_not_just_the_repo(self):
        # The failing host DID have the repository, at a different digest.
        spec, root = self._spec("openbench-tb2-demo@sha256:aaa")
        seen = {}

        def runner(ref):
            seen["ref"] = ref
            return "@sha256:bbb" in ref          # host holds a DIFFERENT digest

        self.assertTrue(mq.missing_task_images(spec, root, docker_runner=runner))
        self.assertIn("@sha256:aaa", seen["ref"])
