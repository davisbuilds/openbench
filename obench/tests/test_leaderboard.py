#!/usr/bin/env python3
"""Tests for the verified-bundle static leaderboard."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import textwrap
import unittest

from obench import leaderboard
from obench import publish
from obench import report_page


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


def _row(harness, task, trial, success, *, model="model-x", tokens=100):
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
        "tokens_input_uncached": tokens - 20,
        "tokens_output": 20,
        "tokens_cache_read": 50,
        "tokens": tokens,
        "token_basis": "vendor_split",
        "harness_version": "1.0",
        "timeout_s": 60,
        "completed": True,
        "candidate_provenance": None,
    }


def _write_results(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _publish_bundle(out_dir, tasks_dir, rows, *, title="Test card"):
    results = os.path.join(out_dir, "_src.jsonl")
    _write_results(results, rows)
    return publish.create_bundle(
        results, out_dir, tasks_dirs=[tasks_dir], title=title,
    )


class TaskSetDigestTests(unittest.TestCase):
    def test_stable_order(self):
        prov = {
            "tasks": [
                {"task": "b", "content_digest": "dd"},
                {"task": "a", "content_digest": "cc"},
            ]
        }
        d1 = leaderboard.task_set_digest(prov)
        d2 = leaderboard.task_set_digest({
            "tasks": list(reversed(prov["tasks"])),
        })
        self.assertEqual(d1, d2)
        expected = hashlib.sha256(b"a:cc\nb:dd").hexdigest()
        self.assertEqual(d1, expected)


class CaveatLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = self.tmp.name

    def test_leaderboard_toml(self):
        _write(
            os.path.join(self.bundle, "leaderboard.toml"),
            'caveats = ["one-shot vs agentic"]\n',
        )
        self.assertEqual(
            leaderboard.load_bundle_caveats(self.bundle),
            ["one-shot vs agentic"],
        )

    def test_index_html_caveats_section(self):
        _write(
            os.path.join(self.bundle, "index.html"),
            textwrap.dedent("""\
                <section id="caveats">
                <ul>
                <li><strong>Mode:</strong> one-shot vs agentic</li>
                </ul>
                </section>
            """),
        )
        caveats = leaderboard.load_bundle_caveats(self.bundle)
        self.assertEqual(len(caveats), 1)
        self.assertIn("one-shot", caveats[0])


class AggregateBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = os.path.join(self.tmp.name, "tasks")
        _make_task(self.tasks, "alpha")
        _make_task(self.tasks, "beta")

    def test_groups_by_harness_model_not_harness_alone(self):
        bundle = os.path.join(self.tmp.name, "bundle-a")
        rows = []
        for trial in (1, 2):
            rows.append(_row("pi", "alpha", trial, True, model="m1", tokens=200))
            rows.append(_row("pi", "alpha", trial, False, model="m2", tokens=100))
            rows.append(_row("null", "alpha", trial, False, model="m1", tokens=50))
        _publish_bundle(bundle, self.tasks, rows)
        agg = leaderboard.aggregate_bundle(bundle, kind="release")
        self.assertIsNotNone(agg)
        keys = {(a["harness"], a["model"]) for a in agg["arms"]}
        self.assertEqual(keys, {("pi", "m1"), ("pi", "m2"), ("null", "m1")})
        by_key = {(a["harness"], a["model"]): a for a in agg["arms"]}
        self.assertEqual(by_key[("pi", "m1")]["solved"], 2)
        self.assertEqual(by_key[("pi", "m1")]["n"], 2)
        self.assertAlmostEqual(by_key[("pi", "m1")]["solve_rate"], 1.0)
        self.assertEqual(len(by_key[("pi", "m1")]["wilson95"]), 2)

    def test_effective_tokens_and_basis(self):
        bundle = os.path.join(self.tmp.name, "bundle-tok")
        rows = [
            _row("pi", "alpha", 1, True, tokens=100),
            _row("pi", "alpha", 2, True, tokens=300),
            _row("null", "alpha", 1, False, tokens=10),
            _row("null", "alpha", 2, False, tokens=10),
        ]
        _publish_bundle(bundle, self.tasks, rows)
        agg = leaderboard.aggregate_bundle(bundle, kind="release")
        pi = next(a for a in agg["arms"] if a["harness"] == "pi")
        # report.py style: sum(tokens on solved rows) / solves
        self.assertAlmostEqual(pi["effective_tokens_per_solve"], 200.0)
        self.assertIn("self-reported", pi["token_bases"])


class BuildLeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = os.path.join(self.tmp.name, "docs")
        self.tasks = os.path.join(self.tmp.name, "tasks")
        _make_task(self.tasks, "alpha")
        os.makedirs(os.path.join(self.site, "releases"), exist_ok=True)

    def _release_bundle(self, rid, rows, *, title=None, date="2026-07-20"):
        bundle = os.path.join(self.site, "releases", rid)
        _publish_bundle(bundle, self.tasks, rows, title=title or rid)
        _write(
            os.path.join(bundle, "leaderboard.toml"),
            'caveats = ["fixture caveat"]\n',
        )
        return {
            "id": rid,
            "title": title or rid,
            "date": date,
            "models": sorted({r["model"] for r in rows}),
            "path": f"releases/{rid}/index.html",
        }

    def test_does_not_blend_bundles(self):
        rows_a = [
            _row("pi", "alpha", 1, True),
            _row("pi", "alpha", 2, True),
            _row("null", "alpha", 1, False),
            _row("null", "alpha", 2, False),
        ]
        rows_b = [
            _row("pi", "alpha", 1, False),
            _row("pi", "alpha", 2, False),
            _row("codex", "alpha", 1, True),
            _row("codex", "alpha", 2, True),
        ]
        rel_a = self._release_bundle("bundle-a", rows_a, date="2026-07-19")
        rel_b = self._release_bundle("bundle-b", rows_b, date="2026-07-20")
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([rel_a, rel_b], indent=2) + "\n",
        )
        _write(os.path.join(self.site, "community.json"), "[]\n")

        doc = leaderboard.build_leaderboard(self.site)
        self.assertEqual(doc["bundle_count"], 2)
        ids = [b["id"] for b in doc["bundles"]]
        self.assertEqual(ids, ["bundle-b", "bundle-a"])  # date desc, then id

        by_id = {b["id"]: b for b in doc["bundles"]}
        # pi is perfect in A and zero in B — must not merge into one arm.
        pi_a = next(a for a in by_id["bundle-a"]["arms"] if a["harness"] == "pi")
        pi_b = next(a for a in by_id["bundle-b"]["arms"] if a["harness"] == "pi")
        self.assertAlmostEqual(pi_a["solve_rate"], 1.0)
        self.assertAlmostEqual(pi_b["solve_rate"], 0.0)
        self.assertTrue(by_id["bundle-a"]["has_caveats"])
        self.assertIn("fixture caveat", by_id["bundle-a"]["caveats"])

        html_page = leaderboard.render_leaderboard_html(doc)
        self.assertIn("Comparability", html_page)
        self.assertIn("never mixed", html_page.lower() + doc["methodology_note"].lower())
        self.assertIn("bundle-a", html_page)
        self.assertIn("bundle-b", html_page)
        self.assertIn("pi × model-x", html_page)

    def test_dedupes_identical_results_sha(self):
        rows = [
            _row("pi", "alpha", 1, True),
            _row("null", "alpha", 1, False),
        ]
        rel = self._release_bundle("official", rows)
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([rel], indent=2) + "\n",
        )
        # Community copy with same results bytes → same sha after publish... 
        # Copy the release bundle tree into community.
        import shutil
        community = os.path.join(self.site, "community", "seed-copy")
        shutil.copytree(
            os.path.join(self.site, "releases", "official"), community,
        )
        _write(
            os.path.join(self.site, "community.json"),
            json.dumps([{
                "id": "seed-copy",
                "submitter": "openbench",
                "date": "2026-07-20",
                "claim": "copy",
                "title": "Copy",
                "path": "community/seed-copy/index.html",
            }], indent=2) + "\n",
        )
        doc = leaderboard.build_leaderboard(self.site)
        self.assertEqual(doc["bundle_count"], 1)
        self.assertEqual(doc["bundles"][0]["id"], "official")
        aliases = doc["bundles"][0]["also_seen_as"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["id"], "seed-copy")

    def test_skips_html_only_releases(self):
        html_only = {
            "id": "html-only",
            "title": "HTML only",
            "date": "2026-07-01",
            "models": ["m"],
            "path": "releases/html-only/index.html",
        }
        os.makedirs(os.path.join(self.site, "releases", "html-only"), exist_ok=True)
        _write(os.path.join(self.site, "releases", "html-only", "index.html"), "<html></html>")
        rows = [
            _row("pi", "alpha", 1, True),
            _row("null", "alpha", 1, False),
        ]
        rel = self._release_bundle("with-jsonl", rows)
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([html_only, rel], indent=2) + "\n",
        )
        _write(os.path.join(self.site, "community.json"), "[]\n")
        doc = leaderboard.build_leaderboard(self.site)
        self.assertEqual(doc["bundle_count"], 1)
        self.assertEqual(doc["skipped"][0]["id"], "html-only")

    def test_write_leaderboard_and_index_link(self):
        rows = [
            _row("pi", "alpha", 1, True),
            _row("null", "alpha", 1, False),
        ]
        rel = self._release_bundle("only", rows)
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([rel], indent=2) + "\n",
        )
        _write(os.path.join(self.site, "community.json"), "[]\n")
        info = leaderboard.write_leaderboard(self.site, refresh_index=True)
        self.assertTrue(os.path.isfile(info["html_path"]))
        self.assertTrue(os.path.isfile(info["json_path"]))
        with open(os.path.join(self.site, "index.html"), encoding="utf-8") as fh:
            index = fh.read()
        self.assertIn("leaderboard.html", index)
        with open(info["json_path"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["bundle_count"], 1)

    def test_deterministic_json(self):
        rows = [
            _row("pi", "alpha", 1, True),
            _row("null", "alpha", 1, False),
        ]
        rel = self._release_bundle("det", rows)
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([rel], indent=2) + "\n",
        )
        _write(os.path.join(self.site, "community.json"), "[]\n")
        a = json.dumps(
            leaderboard.build_leaderboard(self.site),
            indent=2, sort_keys=True, ensure_ascii=False,
        )
        b = json.dumps(
            leaderboard.build_leaderboard(self.site),
            indent=2, sort_keys=True, ensure_ascii=False,
        )
        self.assertEqual(a, b)


class SiteIndexLeaderboardLinkTests(unittest.TestCase):
    def test_site_index_links_leaderboard(self):
        html = report_page._site_index([])
        self.assertIn('href="leaderboard.html"', html)
        self.assertIn("Leaderboard", html)


if __name__ == "__main__":
    unittest.main()
