#!/usr/bin/env python3
"""Tests for the unified harness + gateway leaderboard site."""

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


class GatewayFamilyTests(_SiteFixture):
    def test_missing_gateway_root_is_empty_not_an_error(self):
        doc = site.build_board(self.site_dir)
        self.assertEqual(doc["gateway"]["bundle_count"], 0)
        self.assertEqual(doc["gateway"]["bundles"], [])

    def test_non_gateway_directory_is_reported_as_skipped(self):
        bundle = os.path.join(self.site_dir, "router", "not-a-bundle")
        _write(os.path.join(bundle, "provenance.json"),
               json.dumps({"bundle_kind": "harness"}))
        doc = site.build_board(self.site_dir)
        self.assertEqual(doc["gateway"]["bundle_count"], 0)
        self.assertEqual(
            [s["reason"] for s in doc["gateway"]["skipped"]],
            ["not a router_bench bundle"],  # on-disk kind is unchanged
        )

    def test_tampered_bundle_fails_verification(self):
        bundle = os.path.join(self.site_dir, "router", "tampered")
        _write(os.path.join(bundle, "provenance.json"), json.dumps({
            "schema_version": 1,
            "bundle_kind": "router_bench",
            "artifacts": {"results.jsonl": "0" * 64},
        }))
        _write(os.path.join(bundle, "results.jsonl"), "{}\n")
        self.assertIsNotNone(site.gateway_verification_error(bundle))
        doc = site.build_board(self.site_dir)
        self.assertEqual(doc["gateway"]["bundle_count"], 0)


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
    def test_page_loads_no_external_resources(self):
        """Self-contained means no resource *loads*, not no links.

        A community bundle legitimately links its source on github.com, so
        forbidding the substring "https://" outright would be a false
        invariant that only passes while no fixture has a link.
        """
        doc = site.build_board(self.site_dir)
        page = site.render_board_html(doc)
        self.assertIn("<!doctype html>", page)
        for forbidden in (
            "<script src", "<link rel=\"stylesheet",
            "<img", "<iframe", "@import", "url(http", "src=\"http",
            "fetch(", "XMLHttpRequest", "WebSocket", "cdn.",
        ):
            self.assertNotIn(forbidden, page)

    def test_anchor_links_are_allowed_and_scheme_checked(self):
        """External links are fine; they must still be http(s)."""
        import re
        doc = site.build_board(self.site_dir)
        doc["community"] = [{
            "id": "c1", "title": "Community bundle", "submitter": "someone",
            "path": "community/c1/index.html",
            "link": "https://example.com/proof",
        }]
        page = site.render_board_html(doc)
        self.assertIn('href="https://example.com/proof"', page)
        for href in re.findall(r'href="([^"]+)"', page):
            self.assertFalse(
                href.lower().startswith(("javascript:", "data:", "vbscript:")),
                f"unsafe scheme in href: {href}",
            )

    def test_bundle_supplied_text_is_escaped(self):
        """Titles and caveats come from bundles; they are content, not markup."""
        doc = site.build_board(self.site_dir)
        bundle = doc["harness"]["bundles"][0]
        bundle["title"] = '<img src=x onerror="alert(1)">'
        bundle["caveats"] = ["</table><script>alert(2)</script>"]
        bundle["has_caveats"] = True
        page = site.render_board_html(doc)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<script>alert(2)", page)
        self.assertIn("&lt;img src=x", page)

    def test_tables_are_rendered_without_javascript(self):
        """The script only enhances; the data must be in the document."""
        page = site.render_board_html(site.build_board(self.site_dir))
        head, _, script = page.partition("<script>")
        self.assertIn("<tbody>", head)
        self.assertIn('data-harness="fast"', head)
        self.assertIn('data-harness="slow"', head)
        # Two arms rendered as two real rows, before any JS runs.
        self.assertEqual(head.count("<tr data-harness="), 2)
        self.assertNotIn("<tbody>", script)

    def test_both_families_and_methodology_are_present(self):
        page = site.render_board_html(site.build_board(self.site_dir))
        self.assertIn('id="view-harness"', page)
        self.assertIn('id="view-gateway"', page)
        self.assertIn('id="view-methodology"', page)
        self.assertIn("Gateway Tax", page)

    def test_write_board_emits_the_landing_page_and_data(self):
        info = site.write_board(self.site_dir)
        self.assertEqual(info["html_path"], os.path.join(self.site_dir, "index.html"))
        self.assertTrue(os.path.isfile(info["json_path"]))
        self.assertTrue(os.path.isfile(info["html_path"]))
        with open(info["json_path"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["schema_version"], site.SCHEMA_VERSION)
        self.assertEqual(info["harness_bundles"], 1)
        self.assertEqual(info["gateway_bundles"], 0)

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

    def test_wordmark_is_the_project_name(self):
        self.assertIn("OpenBench", self.page)
        self.assertNotIn('class="cmd"', self.page)
        self.assertNotIn('content:"$ "', self.page)

    def test_both_theme_scopes_are_defined(self):
        # The media query carries the OS preference; the data-theme scopes carry
        # the viewer's toggle and must be able to win in both directions.
        self.assertIn("@media (prefers-color-scheme:dark)", self.page)
        self.assertIn(':root[data-theme="dark"]', self.page)
        self.assertIn(':root[data-theme="light"]', self.page)
        self.assertIn(':root:where(:not([data-theme="light"]))', self.page)

    def test_interval_marks_are_styled_and_placed(self):
        """A class rename that misses the markup silently unstyles the bars."""
        self.assertIn('class="iv"', self.page)
        self.assertIn('.iv .track{', self.page)
        self.assertIn('class="span"', self.page)

    def test_no_separate_leaderboard_page_is_referenced(self):
        self.assertNotIn("leaderboard.html", self.page)

    def test_reduced_motion_and_focus_are_honoured(self):
        self.assertIn("prefers-reduced-motion", self.page)
        self.assertIn(":focus-visible", self.page)

    def test_wide_content_scrolls_inside_its_own_container(self):
        self.assertIn("overflow-x:auto", self.page)
        self.assertIn('class="scroll"', self.page)

    def test_tinted_text_uses_the_text_safe_pole_step(self):
        """Marks may wear the validated hue; text must clear 4.5:1.

        The validated mark colours sit near 3:1, which is fine for a bar and
        not fine for a number. Tinted values use the darker `*-ink` step.
        """
        self.assertIn("--pole-better-ink:", self.page)
        self.assertIn("--pole-worse-ink:", self.page)
        self.assertIn(".dv .val.better{color:var(--pole-better-ink)}", self.page)
        self.assertIn(".dv .val.worse{color:var(--pole-worse-ink)}", self.page)
        # The raw mark hue must never be assigned to a text colour.
        self.assertNotIn(".val.worse{color:var(--pole-worse)}", self.page)

    def test_plot_width_is_budgeted_per_table(self):
        """Four contrast columns cannot each be as wide as a lone one."""
        self.assertIn("--plot-w:", self.page)

    def test_lede_states_coverage_and_draws_no_conclusion(self):
        """The page reports what is covered; the boards carry the results."""
        title, deck, facts = site._lede(site.build_board(self.site_dir))
        self.assertTrue(any("verified bundles" in f for f in facts))
        self.assertTrue(any("countable cells" in f for f in facts))
        # No interpretation of the numbers, and no claimed cause.
        for forbidden in ("because", "due to", "caused by", "proves",
                          "clusters", "spans", "wins", "best", "fastest"):
            self.assertNotIn(forbidden, (title + " " + deck).lower())

    def test_lede_survives_an_empty_site(self):
        with tempfile.TemporaryDirectory() as td:
            title, deck, facts = site._lede(site.build_board(td))
        self.assertTrue(title)
        self.assertTrue(deck)
        self.assertTrue(facts)


class TableOrderingTests(_SiteFixture):
    """A header that claims a sort must be telling the truth."""

    def _rows(self, page, after):
        import re
        chunk = page[page.index(after):]
        chunk = chunk[:chunk.index("</tbody>")]
        return re.findall(r'class="name">([^<]+)<', chunk)

    def test_declared_sort_is_actually_applied(self):
        columns = [
            {"label": "Name", "cls": "name", "type": "str",
             "cell": lambda r: r["n"], "key": lambda r: r["n"]},
            {"label": "Score", "cell": lambda r: str(r["v"]), "key": lambda r: r["v"]},
        ]
        rows = [{"n": "a", "v": 1}, {"n": "b", "v": 9}, {"n": "c", "v": 5}]
        html = site._render_table(columns, rows, "Score")
        self.assertEqual(self._rows(html, "<tbody>"), ["b", "c", "a"])
        self.assertIn('aria-sort="descending"', html)

    def test_ascending_column_sorts_and_labels_ascending(self):
        columns = [
            {"label": "Name", "cls": "name", "type": "str",
             "cell": lambda r: r["n"], "key": lambda r: r["n"]},
            {"label": "Wall", "dir": "asc",
             "cell": lambda r: str(r["v"]), "key": lambda r: r["v"]},
        ]
        rows = [{"n": "a", "v": 9}, {"n": "b", "v": 1}]
        html = site._render_table(columns, rows, "Wall")
        self.assertEqual(self._rows(html, "<tbody>"), ["b", "a"])
        self.assertIn('aria-sort="ascending"', html)

    def test_rows_without_a_value_sink_and_do_not_crash(self):
        columns = [
            {"label": "Name", "cls": "name", "type": "str",
             "cell": lambda r: r["n"], "key": lambda r: r["n"]},
            {"label": "Score", "cell": lambda r: "-", "key": lambda r: r["v"]},
        ]
        rows = [{"n": "a", "v": None}, {"n": "b", "v": 3}]
        html = site._render_table(columns, rows, "Score")
        self.assertEqual(self._rows(html, "<tbody>"), ["b", "a"])

    def test_no_declared_sort_preserves_caller_order(self):
        """The router arms table leads with a control, not a rank."""
        columns = [{"label": "Name", "cls": "name", "type": "str",
                    "cell": lambda r: r["n"], "key": lambda r: r["n"]}]
        rows = [{"n": "z"}, {"n": "a"}]
        html = site._render_table(columns, rows)
        self.assertEqual(self._rows(html, "<tbody>"), ["z", "a"])
        self.assertNotIn("aria-sort", html)

    def test_leading_row_is_marked_for_the_no_javascript_case(self):
        page = site.render_board_html(site.build_board(self.site_dir))
        self.assertIn('class="lead"', page)


class ContrastPlotTests(unittest.TestCase):
    """The gateway-tax cell is the page's only inferential graphic."""

    def _metric(self, estimate, low, high):
        return {"estimate": estimate, "low": low, "high": high}

    def test_interval_spanning_zero_reads_as_no_effect(self):
        cell = site._delta_cell(
            self._metric(-0.028, -0.111, 0.056), site._fmt_pct, True, 0.2)
        self.assertIn("val null", cell)
        self.assertNotIn("better", cell)
        self.assertNotIn("worse", cell)

    def test_direction_uses_the_diverging_poles(self):
        better = site._delta_cell(
            self._metric(0.08, 0.02, 0.14), site._fmt_pct, True, 0.2)
        self.assertIn("val better", better)
        # Lower latency is better, so a positive delta is the "worse" pole.
        worse = site._delta_cell(
            self._metric(3.45, 1.10, 5.90), lambda v: f"{v:.2f}s", False, 6.0)
        self.assertIn("val worse", worse)

    def test_sign_and_interval_survive_without_colour(self):
        cell = site._delta_cell(
            self._metric(3.45, 1.10, 5.90), lambda v: f"{v:.2f}s", False, 6.0)
        self.assertIn("+3.45s", cell)
        self.assertIn("95% CI +1.10s to +5.90s", cell)

    def test_domain_is_shared_across_a_column(self):
        rows = [
            {"d": self._metric(1.0, 0.5, 2.0)},
            {"d": self._metric(-4.0, -6.0, -2.0)},
        ]
        self.assertEqual(site._delta_domain(rows, "d"), 6.0)

    def test_empty_column_domain_never_divides_by_zero(self):
        self.assertEqual(site._delta_domain([], "d"), 1.0)
        self.assertEqual(
            site._delta_domain([{"d": self._metric(None, None, None)}], "d"), 1.0)


class CliTests(_SiteFixture):
    def test_build_subcommand(self):
        from obench.cli import main as cli_main
        code = cli_main(["site", "build", "--site-dir", self.site_dir,
                         "--no-community-dir"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.site_dir, "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(self.site_dir, "board.json")))


if __name__ == "__main__":
    unittest.main()
