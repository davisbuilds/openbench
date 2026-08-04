#!/usr/bin/env python3
"""Tests for the verified-bundle static leaderboard."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import textwrap
import unittest

from obench import leaderboard, site
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
        "tokens_cache_write": 5,
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
        allow_incomplete=True,
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

    def test_native_split_lane_metrics_and_basis(self):
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
        self.assertAlmostEqual(pi["fresh_tokens_per_solve"], 200.0)
        self.assertAlmostEqual(pi["tokens_input_uncached_per_solve"], 180.0)
        self.assertAlmostEqual(pi["tokens_output_per_solve"], 20.0)
        self.assertAlmostEqual(pi["tokens_cache_read_per_solve"], 50.0)
        self.assertAlmostEqual(pi["tokens_cache_write_per_solve"], 5.0)
        self.assertEqual(pi["token_telemetry_source"], "native")
        self.assertEqual(pi["token_telemetry_bases"], ["vendor_split"])
        self.assertEqual(pi["token_telemetry_coverage"]["covered_rows"], 2)
        self.assertEqual(pi["token_telemetry_coverage"]["total_rows"], 2)

    def test_complete_proxy_lane_is_preferred_over_complete_native_lane(self):
        rows = [
            _row("pi", "alpha", 1, True, tokens=100),
            _row("pi", "alpha", 2, True, tokens=300),
        ]
        for index, row in enumerate(rows, start=1):
            row.update({
                "tokens_proxy_input_uncached": 1000 * index,
                "tokens_proxy_output": 100 * index,
                "tokens_proxy_cache_read": 10 * index,
                "tokens_proxy_cache_write": index,
                "token_basis_proxy": "proxy_measured",
            })
        telemetry = leaderboard.token_telemetry_per_solve(rows)
        self.assertEqual(telemetry["token_telemetry_source"], "proxy")
        self.assertEqual(telemetry["token_telemetry_bases"], ["proxy_measured"])
        self.assertEqual(telemetry["fresh_tokens_per_solve"], 1650.0)
        self.assertEqual(telemetry["tokens_cache_read_per_solve"], 15.0)
        self.assertEqual(telemetry["tokens_cache_write_per_solve"], 1.5)

    def test_missing_cache_write_keeps_complete_proxy_core_traffic(self):
        rows = [
            _row("pi", "alpha", 1, True, tokens=100),
            _row("pi", "alpha", 2, True, tokens=300),
        ]
        for index, row in enumerate(rows, start=1):
            row.update({
                "tokens_proxy_input_uncached": 1000 * index,
                "tokens_proxy_output": 100 * index,
                "tokens_proxy_cache_read": 10 * index,
                "tokens_proxy_cache_write": None,
                "token_basis_proxy": "proxy_measured",
            })

        telemetry = leaderboard.token_telemetry_per_solve(rows)

        self.assertEqual(telemetry["token_telemetry_source"], "proxy")
        self.assertEqual(telemetry["fresh_tokens_per_solve"], 1650.0)
        self.assertEqual(telemetry["tokens_cache_read_per_solve"], 15.0)
        self.assertIsNone(telemetry["tokens_cache_write_per_solve"])
        self.assertEqual(
            telemetry["token_telemetry_coverage"]["proxy_covered_rows"],
            2,
        )

    def test_complete_native_lane_is_fallback_for_incomplete_proxy(self):
        rows = [
            _row("pi", "alpha", 1, True, tokens=100),
            _row("pi", "alpha", 2, True, tokens=300),
        ]
        rows[0].update({
            "tokens_proxy_input_uncached": 1000,
            "tokens_proxy_output": 100,
            "tokens_proxy_cache_read": 10,
            "tokens_proxy_cache_write": 1,
            "token_basis_proxy": "proxy_measured",
        })
        telemetry = leaderboard.token_telemetry_per_solve(rows)
        self.assertEqual(telemetry["token_telemetry_source"], "native")
        self.assertEqual(telemetry["fresh_tokens_per_solve"], 200.0)
        self.assertEqual(
            telemetry["token_telemetry_coverage"]["proxy_covered_rows"], 1,
        )

    def test_incomplete_lanes_fail_closed_without_row_mixing(self):
        rows = [
            _row("pi", "alpha", 1, True, tokens=100),
            _row("pi", "alpha", 2, True, tokens=300),
        ]
        rows[0].update({
            "tokens_proxy_input_uncached": 1000,
            "tokens_proxy_output": 100,
            "tokens_proxy_cache_read": 10,
            "tokens_proxy_cache_write": 1,
            "token_basis_proxy": "proxy_measured",
            "tokens_output": None,
        })
        telemetry = leaderboard.token_telemetry_per_solve(rows)
        self.assertIsNone(telemetry["token_telemetry_source"])
        self.assertEqual(telemetry["token_telemetry_bases"], [])
        for field in (
            "fresh_tokens_per_solve",
            "tokens_input_uncached_per_solve",
            "tokens_output_per_solve",
            "tokens_cache_read_per_solve",
            "tokens_cache_write_per_solve",
        ):
            self.assertIsNone(telemetry[field])
        coverage = telemetry["token_telemetry_coverage"]
        self.assertEqual(coverage["covered_rows"], 0)
        self.assertEqual(coverage["proxy_covered_rows"], 1)
        self.assertEqual(coverage["native_covered_rows"], 1)
        self.assertEqual(coverage["total_rows"], 2)

    def test_mismatch_excludes_telemetry_but_retains_visible_grade(self):
        rows = [_row("pi", "alpha", 1, True, tokens=100)]
        rows[0].update({
            "tokens_proxy_input_uncached": 81,
            "tokens_proxy_output": 20,
            "tokens_proxy_cache_read": 50,
            "token_basis_proxy": "proxy_measured",
            "usage_evidence_grade": "harbor_reported_proxy_mismatch",
            "usage_ranking_eligible": False,
            "usage_ranking_exclusion_reason": "proxy_mismatch",
        })

        telemetry = leaderboard.token_telemetry_per_solve(rows)

        self.assertIsNone(telemetry["token_telemetry_source"])
        self.assertIsNone(telemetry["fresh_tokens_per_solve"])
        self.assertEqual(
            telemetry["usage_evidence_labels"], ["Harbor/proxy mismatch"]
        )
        self.assertEqual(
            telemetry["usage_ranking_exclusions"], ["proxy_mismatch"]
        )


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
            "status": "final",
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

        page = site.render_board_html(site.build_board(self.site))
        self.assertIn("Comparability", page)
        self.assertIn("never mixed", page.lower() + doc["methodology_note"].lower())
        self.assertIn("bundle-a", page)
        self.assertIn("bundle-b", page)
        for label in (
            "Fresh tokens/solve",
            "Uncached input/solve",
            "Output/solve",
            "Cache-read/solve",
            "Cache-write/solve",
            "Telemetry source / basis",
            "Telemetry coverage",
        ):
            self.assertIn(label, page)
        self.assertNotIn(">Tokens/solve<", page)
        self.assertNotIn("cache-hit", page.lower())
        # Each bundle keeps its own board rather than merging into one table.
        self.assertEqual(page.count('data-harness="pi"'), 2)

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

    def test_final_release_gate_preserves_community_bundles(self):
        release = self._release_bundle(
            "official", [_row("pi", "alpha", 1, True)]
        )
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([release], indent=2) + "\n",
        )
        community_dir = os.path.join(self.site, "community", "community-one")
        _publish_bundle(
            community_dir,
            self.tasks,
            [_row("codex", "alpha", 1, False)],
            title="Community one",
        )
        _write(
            os.path.join(self.site, "community.json"),
            json.dumps([{
                "id": "community-one",
                "submitter": "openbench",
                "date": "2026-07-21",
                "claim": "community evidence",
                "title": "Community one",
                "path": "community/community-one/index.html",
            }], indent=2) + "\n",
        )

        doc = leaderboard.build_leaderboard(self.site)

        self.assertEqual(doc["bundle_count"], 2)
        self.assertEqual(
            {(bundle["id"], bundle["kind"]) for bundle in doc["bundles"]},
            {("official", "release"), ("community-one", "community")},
        )

    def test_skips_html_only_releases(self):
        html_only = {
            "id": "html-only",
            "status": "final",
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

    def test_skips_results_without_verified_provenance(self):
        release = {
            "id": "unverified", "title": "Unverified", "date": "2026-07-20",
            "status": "final",
            "models": ["model-x"], "path": "releases/unverified/index.html",
        }
        bundle = os.path.join(self.site, "releases", "unverified")
        _write_results(os.path.join(bundle, "results.jsonl"), [
            _row("pi", "alpha", 1, True),
        ])
        _write(os.path.join(bundle, "index.html"), "<html></html>")
        _write(os.path.join(self.site, "releases.json"), json.dumps([release]))
        _write(os.path.join(self.site, "community.json"), "[]\n")
        doc = leaderboard.build_leaderboard(self.site)
        self.assertEqual(doc["bundle_count"], 0)
        self.assertIn("missing provenance.json", doc["skipped"][0]["reason"])
        page = site.render_board_html(site.build_board(self.site))
        self.assertIn("Not ranked", page)
        self.assertIn("missing provenance.json", page)

    def test_rejects_missing_and_non_final_release_status(self):
        rows = [_row("pi", "alpha", 1, True)]
        release = self._release_bundle("candidate", rows)
        manifest_path = os.path.join(self.site, "releases.json")

        release.pop("status")
        _write(manifest_path, json.dumps([release]))
        with self.assertRaisesRegex(ValueError, "publication status missing"):
            leaderboard.build_leaderboard(self.site)

        release["status"] = "draft"
        _write(manifest_path, json.dumps([release]))
        with self.assertRaisesRegex(ValueError, "publication status 'draft'"):
            leaderboard.build_leaderboard(self.site)

    def test_rejects_missing_and_noncanonical_release_path(self):
        release = self._release_bundle(
            "candidate", [_row("pi", "alpha", 1, True)]
        )
        manifest_path = os.path.join(self.site, "releases.json")

        release.pop("path")
        _write(manifest_path, json.dumps([release]))
        with self.assertRaisesRegex(ValueError, "must use canonical path"):
            leaderboard.build_leaderboard(self.site)

        release["path"] = "releases/other/index.html"
        _write(manifest_path, json.dumps([release]))
        with self.assertRaisesRegex(
            ValueError, "releases/candidate/index.html"
        ):
            leaderboard.build_leaderboard(self.site)

    def test_rejects_unlisted_release_directory(self):
        os.makedirs(os.path.join(self.site, "releases", "internal-smoke"))
        _write(os.path.join(self.site, "releases.json"), "[]\n")

        with self.assertRaisesRegex(
            ValueError, "unlisted public release directory.*internal-smoke"
        ):
            leaderboard.build_leaderboard(self.site)

    def test_rejects_final_release_without_canonical_index_file(self):
        release = self._release_bundle(
            "candidate", [_row("pi", "alpha", 1, True)]
        )
        _write(
            os.path.join(self.site, "releases.json"), json.dumps([release])
        )
        os.remove(os.path.join(
            self.site, "releases", "candidate", "index.html"
        ))

        with self.assertRaisesRegex(
            ValueError, "missing its canonical regular file"
        ):
            leaderboard.build_leaderboard(self.site)

    def test_current_tree_drift_is_disclosed_without_erasing_archive(self):
        rows = [
            _row("pi", "alpha", 1, True),
            _row("null", "alpha", 1, False),
        ]
        rel = self._release_bundle("forged-task-tree", rows)
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([rel], indent=2) + "\n",
        )
        _write(os.path.join(self.site, "community.json"), "[]\n")
        _write(
            os.path.join(self.tasks, "alpha", "instruction.md"),
            "changed after publication\n",
        )

        doc = leaderboard.build_leaderboard(self.site)
        self.assertEqual(doc["bundle_count"], 1)
        self.assertEqual(doc["skipped"], [])
        self.assertTrue(doc["bundles"][0]["has_caveats"])
        self.assertTrue(
            any(
                "current checkout for alpha" in caveat
                for caveat in doc["bundles"][0]["caveats"]
            )
        )

    def test_result_hash_tampering_is_still_not_ranked(self):
        rows = [
            _row("pi", "alpha", 1, True),
            _row("null", "alpha", 1, False),
        ]
        rel = self._release_bundle("tampered-results", rows)
        _write(
            os.path.join(self.site, "releases.json"),
            json.dumps([rel], indent=2) + "\n",
        )
        _write(os.path.join(self.site, "community.json"), "[]\n")
        results_path = os.path.join(
            self.site, "releases", rel["id"], "results.jsonl"
        )
        with open(results_path, "a", encoding="utf-8") as handle:
            handle.write("{}\n")

        doc = leaderboard.build_leaderboard(self.site)

        self.assertEqual(doc["bundle_count"], 0)
        self.assertIn("results_sha256", doc["skipped"][0]["reason"])

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
        # Kept as a shim: it now builds the same artifacts as `obench site build`.
        info = leaderboard.write_leaderboard(self.site)
        self.assertEqual(info["html_path"], os.path.join(self.site, "index.html"))
        self.assertTrue(os.path.isfile(info["html_path"]))
        self.assertTrue(os.path.isfile(info["json_path"]))
        with open(info["json_path"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["harness"]["bundle_count"], 1)
        self.assertEqual(info["bundle_count"], 1)

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


class LandingPageTests(unittest.TestCase):
    def test_landing_page_is_the_board(self):
        """There is no separate leaderboard page: index.html is the board."""
        with tempfile.TemporaryDirectory() as td:
            page = site.render_board_html(site.build_board(td))
        self.assertIn('id="view-harness"', page)
        self.assertIn('id="view-gateway"', page)
        self.assertNotIn("leaderboard.html", page)


if __name__ == "__main__":
    unittest.main()
