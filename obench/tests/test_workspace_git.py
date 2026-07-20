#!/usr/bin/env python3
"""Tests for git-mode workspace.toml materialization."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import warnings

from obench import workspace as ws


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class GitRepoFixture:
    """Throwaway git repo under a temp root."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        _git(root, "init")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        # Avoid noisy default-branch variance across git versions.
        _git(root, "checkout", "-b", "main")
        _write(os.path.join(root, "README.md"), "root\n")
        _write(os.path.join(root, "services", "billing", "app.py"), "VALUE = 1\n")
        _write(os.path.join(root, "services", "billing", "readme.txt"), "billing\n")
        _write(os.path.join(root, "services", "other", "x.txt"), "other\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "initial")
        self.sha = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True,
        ).strip()


class WorkspaceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_ws_schema_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task = os.path.join(self.tmp, "task")
        os.makedirs(self.task)

    def test_both_workspace_and_toml_is_error(self):
        os.makedirs(os.path.join(self.task, "workspace"))
        _write(os.path.join(self.task, "workspace.toml"), 'kind = "git"\nref = "HEAD"\n')
        with self.assertRaisesRegex(ws.WorkspaceError, "both workspace/"):
            ws.resolve_workspace_mode(self.task)

    def test_neither_workspace_nor_toml_is_error(self):
        with self.assertRaisesRegex(ws.WorkspaceError, "neither"):
            ws.resolve_workspace_mode(self.task)

    def test_missing_ref_is_error(self):
        _write(os.path.join(self.task, "workspace.toml"), 'kind = "git"\nrepo = "."\n')
        with self.assertRaisesRegex(ws.WorkspaceError, "missing required field: ref"):
            ws.load_git_workspace_spec(self.task)

    def test_empty_repo_is_error(self):
        _write(
            os.path.join(self.task, "workspace.toml"),
            'kind = "git"\nrepo = ""\nref = "HEAD"\n',
        )
        with self.assertRaisesRegex(ws.WorkspaceError, "repo must be a non-empty"):
            ws.load_git_workspace_spec(self.task)

    def test_bad_subdir_rejected(self):
        _write(
            os.path.join(self.task, "workspace.toml"),
            'kind = "git"\nref = "HEAD"\nsubdir = "../escape"\n',
        )
        with self.assertRaisesRegex(ws.WorkspaceError, "subdir"):
            ws.load_git_workspace_spec(self.task)

    def test_unknown_field_rejected(self):
        _write(
            os.path.join(self.task, "workspace.toml"),
            'kind = "git"\nref = "HEAD"\nextra = 1\n',
        )
        with self.assertRaisesRegex(ws.WorkspaceError, "unknown field"):
            ws.load_git_workspace_spec(self.task)

    def test_valid_spec_parses(self):
        _write(
            os.path.join(self.task, "workspace.toml"),
            'kind = "git"\nrepo = "."\nref = "abc"\nsubdir = "a/b"\n'
            'setup = "setup.sh"\ndepth = 1\n',
        )
        spec = ws.load_git_workspace_spec(self.task)
        self.assertEqual(spec.repo, ".")
        self.assertEqual(spec.ref, "abc")
        self.assertEqual(spec.subdir, "a/b")
        self.assertEqual(spec.setup, "setup.sh")
        self.assertEqual(spec.depth, 1)


class WorkspaceStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_ws_stage_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = GitRepoFixture(os.path.join(self.tmp, "repo"))
        self.task = os.path.join(self.repo.root, ".openbench", "tasks", "demo")
        os.makedirs(self.task)
        self.dest = os.path.join(self.tmp, "dest")
        os.makedirs(self.dest)

    def _write_toml(self, body):
        _write(os.path.join(self.task, "workspace.toml"), body)

    def test_stage_local_repo_subdir_and_sha_provenance(self):
        self._write_toml(
            f'kind = "git"\nrepo = "."\nref = "{self.repo.sha}"\n'
            'subdir = "services/billing"\n'
        )
        before = subprocess.check_output(
            ["git", "-C", self.repo.root, "worktree", "list"], text=True,
        )
        prov = ws.materialize_workspace(self.task, self.dest)
        after = subprocess.check_output(
            ["git", "-C", self.repo.root, "worktree", "list"], text=True,
        )
        self.assertEqual(before, after, "source worktree list must be unchanged")
        self.assertEqual(prov["kind"], "git")
        self.assertEqual(prov["repo"], ".")
        self.assertEqual(prov["ref"], self.repo.sha)
        self.assertEqual(prov["resolved_sha"], self.repo.sha)
        self.assertEqual(prov["subdir"], "services/billing")
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "app.py")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "services")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, ".git")))
        with open(os.path.join(self.dest, "app.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 1\n")

    def test_unknown_ref_clear_error(self):
        self._write_toml('kind = "git"\nrepo = "."\nref = "does-not-exist-ref"\n')
        with self.assertRaisesRegex(ws.WorkspaceError, "unknown git ref"):
            ws.materialize_workspace(self.task, self.dest)

    def test_not_a_git_repo_clear_error(self):
        orphan = os.path.join(self.tmp, "orphan_task")
        os.makedirs(orphan)
        _write(
            os.path.join(orphan, "workspace.toml"),
            'kind = "git"\nrepo = "."\nref = "HEAD"\n',
        )
        # Place orphan outside any git repo.
        bare = os.path.join(self.tmp, "notgit")
        os.makedirs(bare)
        orphan2 = os.path.join(bare, "task")
        os.makedirs(orphan2)
        _write(
            os.path.join(orphan2, "workspace.toml"),
            'kind = "git"\nrepo = "."\nref = "HEAD"\n',
        )
        with self.assertRaisesRegex(ws.WorkspaceError, "not a git repository"):
            ws.materialize_workspace(orphan2, self.dest)

    def test_missing_subdir_clear_error(self):
        self._write_toml(
            f'kind = "git"\nrepo = "."\nref = "{self.repo.sha}"\n'
            'subdir = "services/missing"\n'
        )
        with self.assertRaisesRegex(ws.WorkspaceError, "subdir"):
            ws.materialize_workspace(self.task, self.dest)

    def test_branch_ref_warns_and_resolves_sha(self):
        self._write_toml('kind = "git"\nrepo = "."\nref = "main"\nsubdir = "services/billing"\n')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            prov = ws.materialize_workspace(self.task, self.dest)
        self.assertTrue(any("not a full 40-char commit SHA" in str(w.message) for w in caught))
        self.assertEqual(prov["resolved_sha"], self.repo.sha)
        self.assertEqual(prov["ref"], "main")

    def test_setup_script_success(self):
        self._write_toml(
            f'kind = "git"\nrepo = "."\nref = "{self.repo.sha}"\n'
            'subdir = "services/billing"\nsetup = "setup.sh"\n'
        )
        _write(
            os.path.join(self.task, "setup.sh"),
            "#!/usr/bin/env bash\nset -eu\necho prepared > prepared.txt\n"
            'test -n "${TASK_DIR}"\n',
        )
        os.chmod(os.path.join(self.task, "setup.sh"), 0o755)
        ws.materialize_workspace(self.task, self.dest)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "prepared.txt")))

    def test_setup_script_failure_is_error(self):
        self._write_toml(
            f'kind = "git"\nrepo = "."\nref = "{self.repo.sha}"\n'
            'subdir = "services/billing"\nsetup = "setup.sh"\n'
        )
        _write(os.path.join(self.task, "setup.sh"), "#!/usr/bin/env bash\nexit 7\n")
        os.chmod(os.path.join(self.task, "setup.sh"), 0o755)
        with self.assertRaisesRegex(ws.WorkspaceError, "setup script.*exit 7"):
            ws.materialize_workspace(self.task, self.dest)

    def test_no_litter_after_failure(self):
        self._write_toml(
            f'kind = "git"\nrepo = "."\nref = "{self.repo.sha}"\n'
            'subdir = "services/billing"\nsetup = "setup.sh"\n'
        )
        _write(os.path.join(self.task, "setup.sh"), "#!/usr/bin/env bash\nexit 1\n")
        before_wt = subprocess.check_output(
            ["git", "-C", self.repo.root, "worktree", "list"], text=True,
        )
        before_entries = set(os.listdir(self.tmp))
        with self.assertRaises(ws.WorkspaceError):
            ws.materialize_workspace(self.task, self.dest)
        after_wt = subprocess.check_output(
            ["git", "-C", self.repo.root, "worktree", "list"], text=True,
        )
        self.assertEqual(before_wt, after_wt)
        # No new top-level temp dirs left under our fixture root (dest may be
        # partially filled, but no clone litter).
        after_entries = set(os.listdir(self.tmp))
        self.assertEqual(before_entries, after_entries)

    def test_snapshot_mode_unaffected(self):
        snap_task = os.path.join(self.tmp, "snap_task")
        os.makedirs(os.path.join(snap_task, "workspace"))
        _write(os.path.join(snap_task, "workspace", "a.txt"), "hi\n")
        dest = os.path.join(self.tmp, "snap_dest")
        os.makedirs(dest)
        prov = ws.materialize_workspace(snap_task, dest)
        self.assertIsNone(prov)
        with open(os.path.join(dest, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hi\n")


class InitGitRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_init_git_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._cwd)

    def test_init_task_git_ref_writes_toml(self):
        from obench import init

        path = init.init_task("gdemo", git_ref="deadbeef", git_subdir="pkg")
        toml_path = os.path.join(path, "workspace.toml")
        self.assertTrue(os.path.isfile(toml_path))
        self.assertFalse(os.path.isdir(os.path.join(path, "workspace")))
        with open(toml_path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn('ref = "deadbeef"', body)
        self.assertIn('subdir = "pkg"', body)

    def test_cli_rejects_from_and_git_ref(self):
        from obench import init

        with self.assertRaises(SystemExit) as ctx:
            init.main(["--task", "x", "--from", "y", "--git-ref", "HEAD"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
