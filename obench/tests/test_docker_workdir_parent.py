#!/usr/bin/env python3
"""Container-bound workspaces must never be staged in macOS's default tmpdir.

Docker Desktop shares /var/folders into its VM; colima does not. A workspace
staged there bind-mounts as an empty directory, so every checker test fails on
missing files while the host workspace looks perfectly correct.

This cost two separate debugging sessions. obench.run was fixed first (hence
OPENBENCH_DOCKER_TMPDIR), and obench.validate_tasks then hit the identical wall
on its own code path: 5 of 6 tb-mid tasks failed their SOLUTION stage on the
colima host and passed on the Docker Desktop host, purely from this path
choice -- which read as broken tasks, not a broken mount.

These tests pin the two things that actually matter: the policy resolves inside
the shared tree, and every module that stages a bind-mounted workspace goes
through it rather than calling mkdtemp bare.
"""

import ast
import os
import tempfile
import unittest

from obench import paths


class DockerWorkdirParentTests(unittest.TestCase):
    def test_parent_is_not_the_system_tmpdir(self):
        parent = paths.docker_workdir_parent()
        self.assertTrue(os.path.isdir(parent))
        system_tmp = os.path.realpath(tempfile.gettempdir())
        self.assertFalse(
            os.path.realpath(parent).startswith(system_tmp),
            f"{parent} lives under the system tmpdir ({system_tmp}); on colima "
            "this bind-mounts into containers as an EMPTY directory")

    def test_env_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "shared")
            prev = os.environ.get("OPENBENCH_DOCKER_TMPDIR")
            os.environ["OPENBENCH_DOCKER_TMPDIR"] = target
            try:
                self.assertEqual(paths.docker_workdir_parent(), target)
                self.assertTrue(os.path.isdir(target))
            finally:
                if prev is None:
                    os.environ.pop("OPENBENCH_DOCKER_TMPDIR", None)
                else:
                    os.environ["OPENBENCH_DOCKER_TMPDIR"] = prev

    def test_stagers_never_call_mkdtemp_without_a_dir(self):
        # obench.run stages the agent's workspace; obench.validate_tasks stages
        # the polarity-check workspace. Both are bind-mounted into the task
        # image, so neither may use tempfile's default location.
        for module in ("run", "validate_tasks"):
            path = os.path.join(paths.PACKAGE_DIR, f"{module}.py")
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", None) != "mkdtemp":
                    continue
                kwargs = {kw.arg for kw in node.keywords}
                self.assertIn(
                    "dir", kwargs,
                    f"{module}.py:{node.lineno}: mkdtemp() with no dir= lands "
                    "in /var/folders, which colima hides from containers; "
                    "pass dir=docker_workdir_parent()")


if __name__ == "__main__":
    unittest.main()
