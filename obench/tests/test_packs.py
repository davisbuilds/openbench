#!/usr/bin/env python3
"""Tests for versioned task packs (pack.toml, install layout, digests)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

from obench import packs
from obench.publish import DIGEST_SCHEME_CURRENT, task_content_digest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SUBPROC_ENV = {
    **os.environ,
    "PYTHONPATH": _REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
}


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _make_task(root, name, *, with_solution=True, with_provenance=True):
    task_dir = os.path.join(root, name)
    os.makedirs(os.path.join(task_dir, "workspace"), exist_ok=True)
    _write(os.path.join(task_dir, "instruction.md"), f"# {name}\nDo the thing.\n")
    checker = os.path.join(task_dir, "checker.sh")
    _write(checker, "#!/bin/sh\ntest -f greeting.txt\n")
    os.chmod(checker, 0o755)
    _write(os.path.join(task_dir, "workspace", "README"), "start\n")
    if with_solution:
        os.makedirs(os.path.join(task_dir, "solution"), exist_ok=True)
        _write(os.path.join(task_dir, "solution", "greeting.txt"), "hello\n")
    if with_provenance:
        _write(os.path.join(task_dir, "PROVENANCE.md"), "synthetic test task\n")
    return task_dir


def _make_pack(
    root,
    *,
    org="acme",
    name="smoke",
    version="1.0.0",
    tasks=None,
    task_names=("alpha", "beta"),
    explicit_tasks=False,
):
    os.makedirs(root, exist_ok=True)
    for t in task_names:
        _make_task(root, t)
    body = textwrap.dedent(f"""\
        org = "{org}"
        name = "{name}"
        version = "{version}"
        description = "Synthetic pack"
        license = "Apache-2.0"
    """)
    if explicit_tasks:
        rendered = ", ".join(f'"{t}"' for t in (tasks or task_names))
        body += f"tasks = [{rendered}]\n"
    _write(os.path.join(root, "pack.toml"), body)
    return root


class ParseSpecTests(unittest.TestCase):
    def test_org_name_version(self):
        p = packs.parse_pack_spec("acme/smoke@1.2.3")
        self.assertEqual(p, {"org": "acme", "name": "smoke", "version": "1.2.3"})

    def test_org_name_without_version(self):
        p = packs.parse_pack_spec("acme/smoke")
        self.assertEqual(p["version"], None)

    def test_pre_release_version(self):
        p = packs.parse_pack_spec("acme/smoke@1.0.0-beta.1")
        self.assertEqual(p["version"], "1.0.0-beta.1")

    def test_invalid_spec(self):
        with self.assertRaises(packs.PackError):
            packs.parse_pack_spec("not-a-spec")
        with self.assertRaises(packs.PackError):
            packs.parse_pack_spec("acme/smoke@not-semver")


class PackTomlTests(unittest.TestCase):
    def test_load_and_auto_discover(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pack(td, explicit_tasks=False)
            meta = packs.load_pack_toml(os.path.join(td, "pack.toml"))
            self.assertEqual(meta["org"], "acme")
            self.assertEqual(meta["name"], "smoke")
            self.assertIsNone(meta["tasks"])
            names = packs.discover_pack_tasks(td, meta)
            self.assertEqual(names, ["alpha", "beta"])

    def test_explicit_task_list(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pack(td, task_names=("alpha", "beta", "gamma"), explicit_tasks=True,
                       tasks=["alpha", "gamma"])
            meta = packs.load_pack_toml(os.path.join(td, "pack.toml"))
            self.assertEqual(packs.discover_pack_tasks(td, meta), ["alpha", "gamma"])

    def test_missing_explicit_task_errors(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pack(td, explicit_tasks=True, tasks=["alpha", "missing"])
            meta = packs.load_pack_toml(os.path.join(td, "pack.toml"))
            with self.assertRaises(packs.PackError):
                packs.discover_pack_tasks(td, meta)

    def test_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pack(td, org="acme", name="smoke", version="1.0.0")
            meta = packs.load_pack_toml(os.path.join(td, "pack.toml"))
            with self.assertRaises(packs.PackError):
                packs.resolve_install_identity("other/smoke@1.0.0", meta)
            with self.assertRaises(packs.PackError):
                packs.resolve_install_identity("acme/smoke@9.9.9", meta)
            ident = packs.resolve_install_identity("acme/smoke", meta)
            self.assertEqual(ident["version"], "1.0.0")


class InitInstallListTests(unittest.TestCase):
    def test_init_scaffold(self):
        with tempfile.TemporaryDirectory() as td:
            path = packs.init_pack(td, org="org", name="pack", version="0.2.0")
            self.assertTrue(os.path.isfile(path))
            meta = packs.load_pack_toml(path)
            self.assertEqual(meta["org"], "org")
            self.assertEqual(meta["version"], "0.2.0")

    def test_install_layout_and_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            packs_root = os.path.join(td, "packs")
            _make_pack(src, org="acme", name="smoke", version="1.0.0")
            info = packs.install_pack(
                "acme/smoke@1.0.0", src, packs_root=packs_root
            )
            dest = info["dest"]
            self.assertEqual(
                dest,
                os.path.join(packs_root, "acme", "smoke", "1.0.0"),
            )
            self.assertTrue(os.path.isfile(os.path.join(dest, "pack.toml")))
            self.assertTrue(os.path.isdir(os.path.join(dest, "alpha")))
            source = packs.load_pack_source(dest)
            self.assertEqual(source["kind"], "dir")
            self.assertEqual(source["identity"], "acme/smoke@1.0.0")
            self.assertEqual(source["digest_scheme"], DIGEST_SCHEME_CURRENT)
            self.assertIn("content_sha256", source)
            self.assertEqual(
                source["task_digests"]["alpha"],
                task_content_digest(
                    os.path.join(dest, "alpha"), scheme=DIGEST_SCHEME_CURRENT
                ),
            )

            listed = packs.list_installed_packs(packs_root)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["identity"], "acme/smoke@1.0.0")
            self.assertEqual(listed[0]["tasks"], ["alpha", "beta"])

    def test_install_warns_without_solution_but_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            os.makedirs(src)
            _write(
                os.path.join(src, "pack.toml"),
                textwrap.dedent("""\
                    org = "acme"
                    name = "soft"
                    version = "0.1.0"
                """),
            )
            _make_task(src, "lonely", with_solution=False, with_provenance=False)
            packs_root = os.path.join(td, "packs")
            info = packs.install_pack("acme/soft@0.1.0", src, packs_root=packs_root)
            self.assertTrue(any(
                f.get("level") == "warn" for f in info["findings"]
            ))

    def test_install_rejects_hard_structure_failure(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            os.makedirs(src)
            _write(
                os.path.join(src, "pack.toml"),
                'org = "acme"\nname = "bad"\nversion = "0.1.0"\n',
            )
            # Task missing checker.sh → hard structure finding.
            task = os.path.join(src, "broken")
            os.makedirs(os.path.join(task, "workspace"), exist_ok=True)
            _write(os.path.join(task, "instruction.md"), "# broken\n")
            _write(os.path.join(task, "workspace", "x"), "x\n")
            with self.assertRaises(packs.PackError):
                packs.install_pack(
                    "acme/bad@0.1.0", src,
                    packs_root=os.path.join(td, "packs"),
                )

    def test_verify_digests(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            packs_root = os.path.join(td, "packs")
            _make_pack(src)
            info = packs.install_pack(
                "acme/smoke@1.0.0", src, packs_root=packs_root
            )
            results = packs.verify_pack(info["dest"])
            self.assertTrue(all(r["ok"] for r in results))
            # Tamper with a task oracle input.
            with open(
                os.path.join(info["dest"], "alpha", "instruction.md"),
                "a", encoding="utf-8",
            ) as fh:
                fh.write("\nextra\n")
            results = packs.verify_pack(info["dest"])
            mismatched = [r for r in results if r["task"] == "alpha"]
            self.assertEqual(len(mismatched), 1)
            self.assertFalse(mismatched[0]["ok"])

    def test_install_from_zip_url_via_local_https_source_classification(self):
        # Materialize https path using a file://-like flow is awkward; exercise
        # archive extract helper through a zip on disk via classify + zip roundtrip.
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            _make_pack(src, version="2.0.0")
            zip_path = os.path.join(td, "pack.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for dirpath, _dns, filenames in os.walk(src):
                    for name in filenames:
                        full = os.path.join(dirpath, name)
                        arc = os.path.join(
                            "pack-root",
                            os.path.relpath(full, src),
                        )
                        zf.write(full, arcname=arc)
            # Use dir install for layout; separately test _extract + _find_pack_root.
            extract = os.path.join(td, "extract")
            os.makedirs(extract)
            packs._extract_archive(zip_path, extract, "https://example.com/pack.zip")
            root = packs._find_pack_root(extract)
            meta = packs.load_pack_toml(os.path.join(root, "pack.toml"))
            self.assertEqual(meta["version"], "2.0.0")

    def test_classify_source(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(packs.classify_source(td), "dir")
        self.assertEqual(
            packs.classify_source("git+https://example.com/r.git@main"), "git"
        )
        self.assertEqual(
            packs.classify_source("https://example.com/p/pack.tar.gz"), "https"
        )
        self.assertEqual(
            packs.classify_source("https://example.com/r.git"), "git"
        )


class CliTests(unittest.TestCase):
    def test_cli_init_install_list_verify(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            packs_root = os.path.join(td, "packs")
            _make_pack(src, org="cli", name="demo", version="0.3.0")
            r = subprocess.run(
                [
                    sys.executable, "-m", "obench.cli", "pack", "install",
                    "cli/demo@0.3.0", "--from", src, "--packs-dir", packs_root,
                ],
                cwd=td, env=_SUBPROC_ENV, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run(
                [
                    sys.executable, "-m", "obench.cli", "pack", "list",
                    "--packs-dir", packs_root, "--json",
                ],
                cwd=td, env=_SUBPROC_ENV, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data[0]["identity"], "cli/demo@0.3.0")
            r = subprocess.run(
                [
                    sys.executable, "-m", "obench.cli", "pack", "verify",
                    "cli/demo@0.3.0", "--packs-dir", packs_root,
                ],
                cwd=td, env=_SUBPROC_ENV, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_cli_init(self):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [
                    sys.executable, "-m", "obench.cli", "pack", "init",
                    "--dir", td, "--org", "z", "--name", "p", "--version", "1.0.0",
                ],
                env=_SUBPROC_ENV, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.isfile(os.path.join(td, "pack.toml")))


class GitSourceTests(unittest.TestCase):
    def test_install_from_local_git_subdir(self):
        with tempfile.TemporaryDirectory() as td:
            repo = os.path.join(td, "repo")
            os.makedirs(repo)
            subprocess.run(
                ["git", "init"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=repo, check=True, capture_output=True,
            )
            pack_src = os.path.join(repo, "packs", "smoke")
            _make_pack(pack_src, org="gitorg", name="smoke", version="0.1.0")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "add pack"],
                cwd=repo, check=True, capture_output=True,
            )
            packs_root = os.path.join(td, "installed")
            info = packs.install_pack(
                "gitorg/smoke@0.1.0",
                repo,
                packs_root=packs_root,
                git_ref="HEAD",
                git_subdir="packs/smoke",
            )
            source = packs.load_pack_source(info["dest"])
            self.assertEqual(source["kind"], "git")
            self.assertEqual(len(source["resolved_sha"]), 40)
            self.assertEqual(source["subdir"], "packs/smoke")


if __name__ == "__main__":
    unittest.main()
