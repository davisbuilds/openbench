#!/usr/bin/env python3
"""Tests for community submission discovery, verify, and site sync."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from obench import community
from obench import publish
from obench import report_page

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SUBPROC_ENV = {
    **os.environ,
    "PYTHONPATH": _REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
}


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _make_task(root, name):
    task_dir = os.path.join(root, name)
    os.makedirs(os.path.join(task_dir, "workspace"), exist_ok=True)
    _write(os.path.join(task_dir, "instruction.md"), f"# {name}\nDo the thing.\n")
    _write(os.path.join(task_dir, "checker.sh"), "#!/bin/sh\nexit 0\n")
    _write(os.path.join(task_dir, "workspace", "main.py"), "print('hi')\n")
    return task_dir


def _row(harness, task, trial, success, *, model="model-x"):
    return {
        "run_id": f"{harness}:{task}:{model}:trial{trial}",
        "harness": harness,
        "model": model,
        "task": task,
        "trial": trial,
        "success": success,
        "score": 1.0 if success else 0.0,
        "failure_class": "solved" if success else "wrong_answer",
        "wall_time_s": 10.0 + trial,
        "tokens_input_uncached": 100,
        "tokens_output": 20,
        "tokens_cache_read": 50,
        "tokens": 120,
        "token_basis": "vendor_split",
        "harness_version": "1.0",
        "timeout_s": 60,
        "completed": True,
        "candidate_provenance": None,
    }


def _bundle_with_submission(community_root, sid, tasks_dir, *, submitter="alice"):
    results = os.path.join(community_root, f"{sid}-results.jsonl")
    with open(results, "w", encoding="utf-8") as fh:
        for trial in (1, 2):
            fh.write(json.dumps(_row("null", "alpha", trial, False)) + "\n")
            fh.write(json.dumps(_row("pi", "alpha", trial, True)) + "\n")
    bundle = os.path.join(community_root, sid)
    publish.create_bundle(
        results, bundle, tasks_dirs=[tasks_dir], title=f"{sid} card",
    )
    _write(
        os.path.join(bundle, "submission.toml"),
        textwrap.dedent(f"""\
            submitter = "{submitter}"
            date = "2026-07-20"
            title = "Example claim"
            claim = "pi beats null on alpha."
            link = "https://example.com/{sid}"
        """),
    )
    return bundle


class SubmissionTomlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_load_submission_toml_ok(self):
        path = os.path.join(self.tmp.name, "submission.toml")
        _write(
            path,
            textwrap.dedent("""\
                submitter = "alice"
                date = "2026-07-20"
                claim = "my claim"
            """),
        )
        meta = community.load_submission_toml(path)
        self.assertEqual(meta["submitter"], "alice")
        self.assertEqual(meta["date"], "2026-07-20")
        self.assertEqual(meta["claim"], "my claim")
        self.assertEqual(meta["title"], "my claim")
        self.assertIsNone(meta["link"])

    def test_reject_bad_handle_and_date(self):
        path = os.path.join(self.tmp.name, "submission.toml")
        _write(path, 'submitter = "-bad"\ndate = "2026-07-20"\nclaim = "x"\n')
        with self.assertRaises(community.CommunityError):
            community.load_submission_toml(path)
        _write(path, 'submitter = "alice"\ndate = "07-20-2026"\nclaim = "x"\n')
        with self.assertRaises(community.CommunityError):
            community.load_submission_toml(path)


class DiscoverVerifySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.makedirs(self.tasks)
        _make_task(self.tasks, "alpha")
        self.community = os.path.join(self.tmp.name, "community")
        os.makedirs(self.community)
        _write(os.path.join(self.community, "README.md"), "# community\n")

    def test_discover_verify_and_sync(self):
        _bundle_with_submission(self.community, "alice-alpha", self.tasks)
        found = community.discover_submissions(self.community)
        self.assertEqual([s["id"] for s in found], ["alice-alpha"])

        checks, code = community.verify_submission(
            found[0], tasks_dirs=[self.tasks]
        )
        self.assertEqual(code, 0)
        self.assertTrue(all(c["status"] == "PASS" for c in checks))

        site = os.path.join(self.tmp.name, "site")
        os.makedirs(site)
        _write(os.path.join(site, "releases.json"), "[]\n")
        info = community.sync_community_to_site(
            self.community, site, tasks_dirs=[self.tasks])
        self.assertEqual(info["count"], 1)
        self.assertTrue(os.path.isfile(os.path.join(site, "community.json")))
        card = os.path.join(site, "community", "alice-alpha", "index.html")
        self.assertTrue(os.path.isfile(card))
        self.assertTrue(
            os.path.isfile(
                os.path.join(site, "community", "alice-alpha", "submission.toml")
            )
        )
        with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
            index = fh.read()
        self.assertIn("Community", index)
        self.assertIn("alice-alpha", index)
        self.assertIn("@alice", index)

    def test_verify_all_fails_on_tamper(self):
        bundle = _bundle_with_submission(
            self.community, "alice-tamper", self.tasks
        )
        with open(os.path.join(bundle, "results.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("{}\n")
        _results, code = community.verify_all(
            self.community, tasks_dirs=[self.tasks]
        )
        self.assertNotEqual(code, 0)

    def test_sync_refuses_tampered_bundle_before_site_mutation(self):
        bundle = _bundle_with_submission(
            self.community, "alice-tamper-sync", self.tasks)
        with open(os.path.join(bundle, "results.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("{}\n")
        site = os.path.join(self.tmp.name, "site")
        with self.assertRaises(community.CommunityError):
            community.sync_community_to_site(
                self.community, site, tasks_dirs=[self.tasks])
        self.assertFalse(os.path.exists(site))

    def test_site_index_includes_community_section(self):
        from obench import site
        html = site._community_section([{
            "id": "bob-demo",
            "submitter": "bob",
            "date": "2026-07-20",
            "claim": "demo claim",
            "title": "Demo title",
            "path": "community/bob-demo/index.html",
            "link": "https://example.com/x",
        }])
        self.assertIn('id="community"', html)
        self.assertIn("Demo title", html)
        self.assertIn("@bob", html)
        self.assertIn("demo claim", html)
        self.assertIn("community/bob-demo/index.html", html)

    def test_cli_community_verify(self):
        _bundle_with_submission(self.community, "alice-cli", self.tasks)
        proc = subprocess.run(
            [
                sys.executable, "-m", "obench.cli", "community", "verify",
                "--community-dir", self.community,
                "--tasks-dir", self.tasks,
            ],
            cwd=self.tmp.name,
            capture_output=True,
            text=True,
            env=_SUBPROC_ENV,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("VERDICT: PASS", proc.stdout)


class SeedSubmissionSmokeTests(unittest.TestCase):
    """Repo-rooted smoke: the checked-in seed must load and list."""

    def test_seed_directory_is_discoverable(self):
        root = os.path.join(_REPO_ROOT, "data", "community")
        if not os.path.isdir(os.path.join(root, "openbench-aider-showcase")):
            self.skipTest("seed submission not present")
        found = community.discover_submissions(root)
        ids = {s["id"] for s in found}
        self.assertIn("openbench-aider-showcase", ids)
        seed = next(s for s in found if s["id"] == "openbench-aider-showcase")
        self.assertEqual(seed["submitter"], "openbench")


if __name__ == "__main__":
    unittest.main()
