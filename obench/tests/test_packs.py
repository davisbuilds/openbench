#!/usr/bin/env python3
"""Tests for versioned task packs (pack.toml, install layout, digests)."""

from __future__ import annotations

import json
import os
import io
import shutil
import tarfile
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
    def test_default_root_discovers_project_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "tasks"))
            nested = os.path.join(td, "src", "pkg")
            os.makedirs(nested)
            self.assertEqual(
                packs.default_packs_root(nested),
                os.path.join(td, ".openbench", "packs"),
            )

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

    def test_verify_fails_closed_without_recorded_digest(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pack(td)
            results = packs.verify_pack(td)
            self.assertTrue(results)
            self.assertTrue(all(r["missing_expected"] for r in results))
            self.assertTrue(all(not r["ok"] for r in results))

    def test_verify_detects_deleted_recorded_task(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            _make_pack(src)
            info = packs.install_pack(
                "acme/smoke@1.0.0", src,
                packs_root=os.path.join(td, "packs"),
            )
            shutil.rmtree(os.path.join(info["dest"], "alpha"))
            results = packs.verify_pack(info["dest"])
            missing = [r for r in results if r.get("task") == "alpha"]
            self.assertEqual(len(missing), 1)
            self.assertFalse(missing[0]["ok"])
            self.assertTrue(missing[0]["missing_member"])

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

    def test_extract_rejects_zip_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "bad.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escaped.txt", "no\n")
            dest = os.path.join(td, "extract")
            os.makedirs(dest)
            with self.assertRaises(packs.PackError):
                packs._extract_archive(
                    archive, dest, "https://example.com/bad.zip")
            self.assertFalse(os.path.exists(os.path.join(td, "escaped.txt")))

    def test_extract_rejects_tar_links(self):
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "bad.tar")
            with tarfile.open(archive, "w") as tf:
                info = tarfile.TarInfo("linked")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                tf.addfile(info, io.BytesIO())
            dest = os.path.join(td, "extract")
            os.makedirs(dest)
            with self.assertRaises(packs.PackError):
                packs._extract_archive(
                    archive, dest, "https://example.com/bad.tar")

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


def _make_harness_pack(root, *, org="acme", name="cli", version="1.0.0"):
    os.makedirs(root, exist_ok=True)
    _write(
        os.path.join(root, "pack.toml"),
        textwrap.dedent(f"""\
            org = "{org}"
            name = "{name}"
            version = "{version}"
            kind = "harness"
            description = "Synthetic harness pack"
            license = "Apache-2.0"
            manifests = ["demo.toml"]
        """),
    )
    _write(
        os.path.join(root, "demo.toml"),
        textwrap.dedent("""\
            kind = "manifest"
            name = "demo-cli"
            isolate_home = true
            unmetered = true
            policy_headless_args = ["run"]
            policy_auto_approve_args = ["--yes"]
            command = ["true", "run", "--yes", "{prompt}"]
            version_command = ["true", "--version"]
        """),
    )
    return root


class HarnessPackTests(unittest.TestCase):
    def test_install_manifest_digests_and_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            packs_root = os.path.join(td, "packs")
            _make_harness_pack(src)
            info = packs.install_pack(
                "acme/cli@1.0.0", src, packs_root=packs_root
            )
            self.assertEqual(info["kind"], "harness")
            self.assertEqual(info["manifests"], ["demo.toml"])
            source = packs.load_pack_source(info["dest"])
            self.assertEqual(source["kind"], "dir")
            self.assertIn("demo.toml", source["spec_sha256"])
            self.assertEqual(
                source["spec_sha256"]["demo.toml"],
                source["manifest_digests"]["demo.toml"],
            )
            self.assertEqual(
                source["manifest_digests"]["demo.toml"],
                packs.manifest_spec_sha256(
                    os.path.join(info["dest"], "demo.toml")
                ),
            )
            resolved = packs.resolve_candidate_ref(
                "acme/cli@1.0.0", packs_root=packs_root
            )
            self.assertEqual(
                resolved, os.path.join(info["dest"], "demo.toml")
            )
            latest = packs.resolve_candidate_ref(
                "acme/cli", packs_root=packs_root
            )
            self.assertEqual(latest, resolved)
            results = packs.verify_pack(info["dest"])
            self.assertTrue(all(r["ok"] for r in results))

    def test_resolve_requires_manifest_when_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            _make_harness_pack(src)
            _write(
                os.path.join(src, "other.toml"),
                textwrap.dedent("""\
                    kind = "manifest"
                    name = "other-cli"
                    isolate_home = true
                    unmetered = true
                    policy_headless_args = ["run"]
                    policy_auto_approve_args = ["--yes"]
                    command = ["true", "run", "--yes", "{prompt}"]
                """),
            )
            _write(
                os.path.join(src, "pack.toml"),
                textwrap.dedent("""\
                    org = "acme"
                    name = "multi"
                    version = "1.0.0"
                    kind = "harness"
                """),
            )
            packs_root = os.path.join(td, "packs")
            packs.install_pack("acme/multi@1.0.0", src, packs_root=packs_root)
            with self.assertRaises(packs.PackError):
                packs.resolve_candidate_ref(
                    "acme/multi@1.0.0", packs_root=packs_root
                )
            path = packs.resolve_candidate_ref(
                "acme/multi@1.0.0:other", packs_root=packs_root
            )
            self.assertTrue(path.endswith("other.toml"))

    def test_load_candidates_pack_ref(self):
        from obench.candidates import load_candidates
        from obench.paths import default_adapters_dir

        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            packs_root = os.path.join(td, "packs")
            _make_harness_pack(src, org="z", name="p", version="0.2.0")
            packs.install_pack("z/p@0.2.0", src, packs_root=packs_root)
            old = os.environ.get("PWD")
            try:
                # resolve uses default_packs_root()=cwd/.openbench/packs unless given
                loaded = load_candidates(
                    ["z/p@0.2.0"],
                    default_adapters_dir(),
                    packs_root=packs_root,
                )
            finally:
                pass
            self.assertIn("demo-cli", loaded)

    def test_reject_invalid_manifest_on_install(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            os.makedirs(src)
            _write(
                os.path.join(src, "pack.toml"),
                'org = "acme"\nname = "bad"\nversion = "0.1.0"\nkind = "harness"\n',
            )
            _write(os.path.join(src, "broken.toml"), 'kind = "manifest"\nname = "x"\n')
            with self.assertRaises(packs.PackError):
                packs.install_pack(
                    "acme/bad@0.1.0", src,
                    packs_root=os.path.join(td, "packs"),
                )


class PacksIndexTests(unittest.TestCase):
    def test_publish_index_upsert_and_site_section(self):
        from obench import report_page

        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            _make_pack(src, org="acme", name="smoke", version="1.0.0")
            site = os.path.join(td, "site")
            os.makedirs(site)
            _write(os.path.join(site, "releases.json"), "[]\n")
            _write(os.path.join(site, "community.json"), "[]\n")
            info = packs.publish_packs_index(
                src, site_dir=site, source="data/packs/smoke"
            )
            self.assertFalse(info["replaced"])
            self.assertTrue(os.path.isfile(info["manifest_path"]))
            entries = packs.load_packs_index(site)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["id"], "acme/smoke")
            self.assertEqual(entries[0]["kind"], "tasks")
            self.assertEqual(entries[0]["source"], "data/packs/smoke")
            self.assertIn("content_sha256", entries[0])

            # Bump version and upsert.
            _make_pack(src, org="acme", name="smoke", version="1.1.0")
            info2 = packs.publish_packs_index(
                src, site_dir=site, source="data/packs/smoke"
            )
            self.assertTrue(info2["replaced"])
            entries = packs.load_packs_index(site)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["latest"], "1.1.0")

            from obench import site
            html = site._packs_section(entries)
            self.assertIn('id="packs"', html)
            self.assertIn("acme/smoke@1.1.0", html)
            self.assertIn("tasks", html)

    def test_cli_publish_index(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            _make_harness_pack(src, org="cli", name="h", version="0.1.0")
            site = os.path.join(td, "site")
            os.makedirs(site)
            _write(os.path.join(site, "releases.json"), "[]\n")
            r = subprocess.run(
                [
                    sys.executable, "-m", "obench.cli", "pack", "publish-index",
                    "--from", src, "--site-dir", site,
                    "--source-url", "data/packs/cli-h",
                ],
                cwd=td, env=_SUBPROC_ENV, capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            with open(os.path.join(site, "packs.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data[0]["kind"], "harness")
            with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
                self.assertIn("packs", fh.read())


class SeededPackSmokeTests(unittest.TestCase):
    """Repo-rooted smoke for committed seed packs under data/packs/."""

    def test_seeded_task_and_harness_packs_load(self):
        task_pack = os.path.join(
            _REPO_ROOT, "data", "packs", "openbench-core-smoke"
        )
        harness_pack = os.path.join(
            _REPO_ROOT, "data", "packs", "openbench-aider"
        )
        if not os.path.isdir(task_pack):
            self.skipTest("seeded task pack missing")
        meta = packs.load_pack_toml(os.path.join(task_pack, "pack.toml"))
        self.assertEqual(meta["kind"], "tasks")
        self.assertEqual(
            packs.discover_pack_tasks(task_pack, meta),
            ["make-it-run", "fix-failing-test"],
        )
        if not os.path.isdir(harness_pack):
            self.skipTest("seeded harness pack missing")
        hmeta = packs.load_pack_toml(os.path.join(harness_pack, "pack.toml"))
        self.assertEqual(hmeta["kind"], "harness")
        self.assertEqual(
            packs.discover_pack_manifests(harness_pack, hmeta),
            ["aider.toml"],
        )


if __name__ == "__main__":
    unittest.main()
