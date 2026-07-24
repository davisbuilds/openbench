#!/usr/bin/env python3
"""Tests for the unified harness + router leaderboard site."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from obench import publish, site


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


def _row(harness, task, trial, success, *, model="model-x", wall=10.0, tokens=100):
    return {
        "run_id": f"{harness}:{task}:{model}:trial{trial}",
        "harness": harness,
        "model": model,
        "task": task,
        "trial": trial,
        "success": success,
        "score": 1.0 if success else 0.0,
        "failure_class": "solved" if success else "wrong_answer",
        "wall_time_s": wall,
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


def _publish_bundle(out_dir, tasks_dir, rows, *, title="Test card"):
    results = os.path.join(out_dir, "_src.jsonl")
    os.makedirs(out_dir, exist_ok=True)
    with open(results, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return publish.create_bundle(results, out_dir, tasks_dirs=[tasks_dir], title=title)


class _SiteFixture(unittest.TestCase):
    """A docs/ root holding one verified two-arm harness bundle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.site_dir = os.path.join(self.root, "docs")
        tasks_dir = os.path.join(self.root, "tasks")
        _make_task(tasks_dir, "alpha")
        _make_task(tasks_dir, "beta")

        rows = []
        for task in ("alpha", "beta"):
            for trial in (1, 2):
                rows.append(_row("fast", task, trial, True, wall=5.0))
                rows.append(_row("slow", task, trial, task == "alpha", wall=40.0))
        _publish_bundle(
            os.path.join(self.site_dir, "releases", "b1"),
            tasks_dir,
            rows,
            title="Two harnesses",
        )
        _write(
            os.path.join(self.site_dir, "releases.json"),
            json.dumps([{
                "id": "b1", "title": "Two harnesses", "date": "2026-07-24",
                "models": ["model-x"], "path": "releases/b1/index.html",
            }]),
        )


class HarnessFamilyTests(_SiteFixture):
    def test_bundle_is_ranked_and_enriched(self):
        doc = site.build_board(self.site_dir, community_dir=None)
        family = doc["harness"]
        self.assertEqual(family["bundle_count"], 1)
        arms = family["bundles"][0]["arms"]
        self.assertEqual([a["harness"] for a in arms], ["fast", "slow"])
        self.assertEqual(arms[0]["solve_rate"], 1.0)
        self.assertEqual(arms[1]["solve_rate"], 0.5)
        # Enrichment: median wall time comes from solved cells only.
        self.assertAlmostEqual(arms[0]["median_wall_s"], 5.0)
        self.assertAlmostEqual(arms[1]["median_wall_s"], 40.0)
        # No configured price for model-x, so cost stays absent rather than 0.
        self.assertIsNone(arms[0]["cost_per_solve_usd"])

    def test_enrichment_matches_published_denominators(self):
        """Enriched rows describe the same cells the solve rate was built from."""
        doc = site.build_board(self.site_dir, community_dir=None)
        for arm in doc["harness"]["bundles"][0]["arms"]:
            self.assertIsNotNone(arm["median_wall_s"])
            self.assertEqual(arm["n"], 4)

    def test_unverified_release_is_skipped_not_ranked(self):
        _write(os.path.join(self.site_dir, "releases", "html-only", "index.html"), "<p>x</p>")
        doc = site.build_board(self.site_dir, community_dir=None)
        self.assertEqual(doc["harness"]["bundle_count"], 1)
        self.assertIn("html-only", [s["id"] for s in doc["harness"]["skipped"]])


class RouterFamilyTests(_SiteFixture):
    def test_missing_router_root_is_empty_not_an_error(self):
        doc = site.build_board(self.site_dir)
        self.assertEqual(doc["router"]["bundle_count"], 0)
        self.assertEqual(doc["router"]["bundles"], [])

    def test_non_router_directory_is_reported_as_skipped(self):
        bundle = os.path.join(self.site_dir, "router", "not-a-bundle")
        _write(os.path.join(bundle, "provenance.json"),
               json.dumps({"bundle_kind": "harness"}))
        doc = site.build_board(self.site_dir)
        self.assertEqual(doc["router"]["bundle_count"], 0)
        self.assertEqual(
            [s["reason"] for s in doc["router"]["skipped"]],
            ["not a router_bench bundle"],
        )

    def test_tampered_bundle_fails_verification(self):
        bundle = os.path.join(self.site_dir, "router", "tampered")
        _write(os.path.join(bundle, "provenance.json"), json.dumps({
            "schema_version": 1,
            "bundle_kind": "router_bench",
            "artifacts": {"results.jsonl": "0" * 64},
        }))
        _write(os.path.join(bundle, "results.jsonl"), "{}\n")
        self.assertIsNotNone(site.router_verification_error(bundle))
        doc = site.build_board(self.site_dir)
        self.assertEqual(doc["router"]["bundle_count"], 0)


class CostBasisTests(unittest.TestCase):
    def _basis(self, name, covered, per_solve=1.0):
        return {
            "attempted_cost_usd": {"estimate": per_solve},
            "cost_per_solve_usd": per_solve,
            "basis_coverage": {"covered_calls": covered, "ratio": 1.0 if covered else 0.0},
        }

    def test_prefers_invoice_over_router_reported(self):
        picked = site._pick_cost({
            "router_reported": self._basis("router_reported", 10),
            "invoice_reconciled": self._basis("invoice_reconciled", 10),
            "frozen_list_estimate": self._basis("frozen_list_estimate", 10),
        })
        self.assertEqual(picked["basis"], "invoice_reconciled")

    def test_skips_bases_with_no_covered_calls(self):
        picked = site._pick_cost({
            "invoice_reconciled": self._basis("invoice_reconciled", 0),
            "router_reported": self._basis("router_reported", 4),
        })
        self.assertEqual(picked["basis"], "router_reported")

    def test_no_covered_basis_returns_none(self):
        self.assertIsNone(site._pick_cost({"router_reported": self._basis("x", 0)}))
        self.assertIsNone(site._pick_cost({}))


class RenderTests(_SiteFixture):
    def test_page_is_self_contained(self):
        doc = site.build_board(self.site_dir)
        page = site.render_board_html(doc)
        self.assertIn("<!doctype html>", page)
        # No third-party assets and no network fetches.
        for forbidden in ("http://", "https://", "fetch(", "cdn."):
            self.assertNotIn(forbidden, page)

    def test_embedded_payload_cannot_close_the_script_element(self):
        doc = site.build_board(self.site_dir)
        doc["harness"]["bundles"][0]["title"] = "</script><img src=x onerror=alert(1)>"
        page = site.render_board_html(doc)
        start = page.index('id="board-data"')
        end = page.index("</script>", start)
        payload = page[page.index(">", start) + 1:end]
        self.assertNotIn("</script>", payload)
        # The escaped payload still parses back to the original string.
        parsed = json.loads(payload.replace("<\\/", "</"))
        self.assertEqual(
            parsed["harness"]["bundles"][0]["title"],
            "</script><img src=x onerror=alert(1)>",
        )

    def test_both_families_and_methodology_are_present(self):
        page = site.render_board_html(site.build_board(self.site_dir))
        self.assertIn('id="view-harness"', page)
        self.assertIn('id="view-router"', page)
        self.assertIn('id="view-methodology"', page)
        self.assertIn("Gateway Tax", page)

    def test_write_board_emits_both_artifacts(self):
        info = site.write_board(self.site_dir)
        self.assertTrue(os.path.isfile(info["json_path"]))
        self.assertTrue(os.path.isfile(info["html_path"]))
        with open(info["json_path"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["schema_version"], site.SCHEMA_VERSION)
        self.assertEqual(info["harness_bundles"], 1)
        self.assertEqual(info["router_bundles"], 0)

    def test_build_is_deterministic(self):
        first = site.write_board(self.site_dir)
        with open(first["json_path"], encoding="utf-8") as fh:
            one = fh.read()
        site.write_board(self.site_dir)
        with open(first["json_path"], encoding="utf-8") as fh:
            two = fh.read()
        self.assertEqual(one, two)


class DesignContractTests(_SiteFixture):
    """The page ships one stylesheet; these are the parts easy to break."""

    def setUp(self):
        super().setUp()
        self.page = site.render_board_html(site.build_board(self.site_dir))

    def test_both_theme_scopes_are_defined(self):
        # The media query carries the OS preference; the data-theme scopes carry
        # the viewer's toggle and must be able to win in both directions.
        self.assertIn("@media (prefers-color-scheme:dark)", self.page)
        self.assertIn(':root[data-theme="dark"]', self.page)
        self.assertIn(':root[data-theme="light"]', self.page)
        self.assertIn(':root:where(:not([data-theme="light"]))', self.page)

    def test_no_stale_interval_class(self):
        """`.ci` was renamed to `.iv`; a stale name silently unstyles the bars."""
        self.assertNotIn('class: "ci"', self.page)
        self.assertIn('class: "iv"', self.page)

    def test_contrast_legend_names_every_tone(self):
        self.assertIn("Gateway better than direct", self.page)
        self.assertIn("Gateway worse than direct", self.page)
        self.assertIn("no detected effect", self.page)

    def test_reduced_motion_and_focus_are_honoured(self):
        self.assertIn("prefers-reduced-motion", self.page)
        self.assertIn(":focus-visible", self.page)

    def test_wide_content_scrolls_inside_its_own_container(self):
        self.assertIn(".scroll{overflow-x:auto}", self.page)


class CliTests(_SiteFixture):
    def test_build_subcommand(self):
        from obench.cli import main as cli_main
        code = cli_main(["site", "build", "--site-dir", self.site_dir,
                         "--no-community-dir"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.site_dir, "board.html")))


if __name__ == "__main__":
    unittest.main()
