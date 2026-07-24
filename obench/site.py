#!/usr/bin/env python3
"""Unified static leaderboard site for both OpenBench benchmark families.

``obench site build`` scans the GitHub Pages root and emits two artifacts:

* ``board.json`` — one machine-readable document covering **Harness Bench**
  (verified ``results.jsonl`` publish bundles, aggregated by
  :mod:`obench.leaderboard`) and **Router Bench** (verified ``router_bench``
  evidence bundles, aggregated by :mod:`obench.router_report`).
* ``board.html`` — a self-contained browsing UI over that document: family
  tabs, per-board sortable tables, model/harness filters, Wilson and bootstrap
  confidence intervals drawn as bars, and the Gateway Tax contrast table.

The page embeds its own data, so it works from ``file://`` with no server, no
build step, and no third-party assets — the same constraints as every other
page this repo publishes.

Comparability rule, unchanged from ``obench leaderboard``: cells from different
bundles are never blended into one score. Each bundle is its own ranked board.
The two families are never merged either — a Router Bench arm is a serving
route, not a harness.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import defaultdict

from . import leaderboard, report_page, stats
from .paths import SOURCE_ROOT

SCHEMA_VERSION = 1

HARNESS_NOTE = (
    "Harness Bench varies the coding-agent harness while holding the model and "
    "task fixed. Each board below is one verified publish bundle. Scores are "
    "never mixed across bundles: different task sets, trial counts, timeout "
    "caps, or run conditions make cross-bundle rankings non-comparable. Within "
    "a bundle, arms are (harness, model) and — when two or more arms exist — "
    "use matched (task, trial) denominators."
)

ROUTER_NOTE = (
    "Router Bench holds the harness, model, provider, sampling, task, and "
    "budget fixed while varying the serving route. The implemented track is "
    "Gateway Tax: one direct baseline arm against one or more gateway arms on "
    "the same canonical model revision, with fallbacks, gateway retries, and "
    "caching disabled. A block counts only when every expected arm is present, "
    "infrastructure-valid, and passes route integrity. Intervals are "
    "task-bootstrap, not Wilson."
)

CROSS_FAMILY_NOTE = (
    "The two families answer different questions and share no denominators. "
    "Do not read a Router Bench arm as another harness, or compare a solve "
    "rate across families."
)

# Preferred cost basis when an arm reports several. Invoice reconciliation is
# ground truth; the router's own number is next; a frozen list price is an
# estimate and is labelled as one in the UI.
COST_BASIS_PREFERENCE = (
    "invoice_reconciled",
    "router_reported",
    "frozen_list_estimate",
)


# --------------------------------------------------------------------------
# Harness Bench
# --------------------------------------------------------------------------


def _load_pricing():
    """Repo price sheet, when present. Missing prices simply omit $/solve."""
    for candidate in (
        os.path.join(SOURCE_ROOT, "prices.json"),
        os.path.join(os.getcwd(), "prices.json"),
    ):
        if os.path.isfile(candidate):
            try:
                return stats.load_pricing(candidate)
            except (OSError, ValueError):
                return {}
    return {}


def _arm_rows(bundle_dir):
    """Regroup a bundle's countable rows by ``(harness, model)``.

    Mirrors :func:`obench.leaderboard.aggregate_bundle` exactly so the enriched
    speed/cost columns describe the same cells as the published solve rate.
    """
    results_path = os.path.join(bundle_dir, "results.jsonl")
    rows = stats.load_rows([results_path])
    countable = stats.filter_rows(rows, [])["countable_rows"]
    fields = ("harness", "model")
    matched, diagnostics = stats.matched_rows(countable, fields)
    table_rows = matched if diagnostics is not None else countable
    grouped = defaultdict(list)
    for row in table_rows:
        key = (str(row.get("harness") or "-"), str(row.get("model") or "-"))
        grouped[key].append(row)
    return grouped


def enrich_harness_arms(bundle, bundle_dir, pricing):
    """Attach median wall time and $/solve to an aggregated bundle's arms."""
    try:
        grouped = _arm_rows(bundle_dir)
    except (OSError, ValueError):
        return bundle
    for arm in bundle["arms"]:
        rows = grouped.get((arm["harness"], arm["model"]))
        if not rows:
            arm["median_wall_s"] = None
            arm["cost_per_solve_usd"] = None
            continue
        solved_rows = [r for r in rows if r.get("success")]
        walls = [
            float(r["wall_time_s"]) for r in solved_rows
            if stats.is_nonnegative_number(r.get("wall_time_s"))
        ]
        arm["median_wall_s"] = stats.median(walls) if walls else None
        arm["cost_per_solve_usd"] = report_page._cost_per_solve(
            rows, len(solved_rows), pricing
        )
    return bundle


def build_harness_family(site_dir, community_dir=None):
    """Aggregated Harness Bench boards, enriched with speed and cost."""
    doc = leaderboard.build_leaderboard(site_dir, community_dir=community_dir)
    pricing = _load_pricing()
    for bundle in doc["bundles"]:
        bundle_dir = _bundle_dir_for(site_dir, community_dir, bundle)
        if bundle_dir:
            enrich_harness_arms(bundle, bundle_dir, pricing)
        bundle["family"] = "harness"
    return {
        "note": HARNESS_NOTE,
        "bundle_count": doc["bundle_count"],
        "bundles": doc["bundles"],
        "skipped": doc.get("skipped") or [],
    }


def _bundle_dir_for(site_dir, community_dir, bundle):
    """Resolve an aggregated bundle back to the directory it was read from."""
    results_rel = bundle.get("results_path") or ""
    if results_rel:
        candidate = os.path.join(site_dir, results_rel)
        if os.path.isfile(candidate):
            return os.path.dirname(candidate)
    for parent in ("releases", "community"):
        candidate = os.path.join(site_dir, parent, bundle["id"])
        if os.path.isfile(os.path.join(candidate, "results.jsonl")):
            return candidate
    if community_dir:
        candidate = os.path.join(community_dir, bundle["id"])
        if os.path.isfile(os.path.join(candidate, "results.jsonl")):
            return candidate
    return None


# --------------------------------------------------------------------------
# Router Bench
# --------------------------------------------------------------------------


def router_verification_error(bundle_dir):
    """Return why a directory is not a verified router bundle, else ``None``."""
    provenance_path = os.path.join(bundle_dir, "provenance.json")
    if not os.path.isfile(provenance_path):
        return "no provenance.json (not a router evidence bundle)"
    try:
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "invalid provenance.json"
    if not isinstance(provenance, dict):
        return "invalid provenance.json"
    if provenance.get("bundle_kind") != "router_bench":
        return "not a router_bench bundle"
    from . import router_publish
    try:
        router_publish.verify_bundle(bundle_dir)
    except Exception as exc:  # noqa: BLE001 - report any verification failure
        return f"bundle verification failed: {exc}"
    return None


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _pick_cost(costs):
    """Best-covered cost basis for an arm, in preference order."""
    if not isinstance(costs, dict):
        return None
    ordered = [b for b in COST_BASIS_PREFERENCE if b in costs]
    ordered += [b for b in sorted(costs) if b not in COST_BASIS_PREFERENCE]
    for basis in ordered:
        entry = costs.get(basis) or {}
        coverage = entry.get("basis_coverage") or {}
        if (coverage.get("covered_calls") or 0) <= 0:
            continue
        return {
            "basis": basis,
            "attempted_cost_usd": (entry.get("attempted_cost_usd") or {}).get("estimate"),
            "cost_per_solve_usd": entry.get("cost_per_solve_usd"),
            "coverage_ratio": coverage.get("ratio"),
        }
    return None


def _metric_dto(metric):
    if not isinstance(metric, dict):
        return {"estimate": None, "low": None, "high": None}
    interval = metric.get("interval") or {}
    return {
        "estimate": metric.get("estimate"),
        "low": interval.get("low"),
        "high": interval.get("high"),
    }


def aggregate_router_bundle(bundle_dir, *, site_dir=None, manifest_entry=None):
    """Aggregate one verified router bundle, or ``None`` when unusable."""
    from . import router_report

    if router_verification_error(bundle_dir) is not None:
        return None
    results_path = os.path.join(bundle_dir, "results.jsonl")
    try:
        rows = _read_jsonl(results_path)
        report = router_report.aggregate(rows)
    except (OSError, ValueError) as exc:
        del exc
        return None

    experiment = {}
    experiment_path = os.path.join(bundle_dir, "experiment.json")
    if os.path.isfile(experiment_path):
        try:
            with open(experiment_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                experiment = loaded
        except (OSError, json.JSONDecodeError):
            experiment = {}

    arm_spec = {
        a.get("arm_id"): a for a in (experiment.get("arms") or [])
        if isinstance(a, dict) and a.get("arm_id")
    }

    arms = []
    for arm_id, arm in (report.get("arms") or {}).items():
        metrics = arm.get("metrics") or {}
        spec = arm_spec.get(arm_id, {})
        arms.append({
            "arm_id": arm_id,
            "role": arm.get("role"),
            "route_kind": spec.get("route_kind"),
            "requested_model": spec.get("requested_model"),
            "requested_provider": spec.get("requested_provider"),
            "solve_rate": _metric_dto(metrics.get("solve_rate")),
            "mean_checker_score": _metric_dto(metrics.get("mean_checker_score")),
            "availability": _metric_dto(metrics.get("availability")),
            "latency_s": _metric_dto(metrics.get("latency_s")),
            "cost": _pick_cost(arm.get("costs")),
        })
    # Baseline first, then slowest-to-fastest is meaningless before sorting in
    # the UI; keep a deterministic order: direct arms first, then arm_id.
    arms.sort(key=lambda a: (0 if a["role"] == "direct" else 1, a["arm_id"]))

    contrasts = []
    for arm_id, contrast in (report.get("paired_contrasts") or {}).items():
        metrics = contrast.get("metrics") or {}
        contrasts.append({
            "arm_id": arm_id,
            "direct_arm": contrast.get("direct_arm"),
            "solve_rate": _metric_dto(metrics.get("solve_rate")),
            "latency_s": _metric_dto(metrics.get("latency_s")),
            "mean_checker_score": _metric_dto(metrics.get("mean_checker_score")),
            "availability": _metric_dto(metrics.get("availability")),
        })
    contrasts.sort(key=lambda c: c["arm_id"])

    entry = dict(manifest_entry or {})
    bundle_id = entry.get("id") or os.path.basename(os.path.normpath(bundle_dir))
    blocks = report.get("blocks") or {}
    return {
        "family": "router",
        "id": bundle_id,
        "kind": entry.get("kind") or "release",
        "title": entry.get("title") or bundle_id,
        "date": entry.get("date") or "",
        "link": entry.get("link"),
        "submitter": entry.get("submitter"),
        "path": entry.get("path"),
        "results_path": (
            leaderboard._rel_under(site_dir, results_path) if site_dir else results_path
        ),
        "track": report.get("track"),
        "harness": experiment.get("harness"),
        "experiment_id": experiment.get("experiment_id"),
        "experiment_digest": report.get("experiment_digest"),
        "execution_lane": experiment.get("execution_lane"),
        "blocks_included": blocks.get("included"),
        "blocks_observed": blocks.get("observed"),
        "blocks_excluded": blocks.get("excluded_by_reason") or {},
        "tasks_included": (report.get("tasks") or {}).get("included"),
        "arms": arms,
        "contrasts": contrasts,
    }


def build_router_family(site_dir, router_dirs=None):
    """Scan router bundle roots and aggregate every verified bundle."""
    site_dir = os.path.abspath(site_dir)
    roots = list(router_dirs or [])
    default_root = os.path.join(site_dir, "router")
    if not roots and os.path.isdir(default_root):
        roots = [default_root]

    manifest = {
        e["id"]: e
        for e in leaderboard._load_manifest_list(os.path.join(site_dir, "router.json"))
        if isinstance(e, dict) and e.get("id")
    }

    bundles = []
    skipped = []
    seen = set()
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            bundle_dir = os.path.join(root, name)
            if not os.path.isdir(bundle_dir):
                continue
            real = os.path.realpath(bundle_dir)
            if real in seen:
                continue
            seen.add(real)
            error = router_verification_error(bundle_dir)
            if error:
                skipped.append({"id": name, "kind": "router", "reason": error})
                continue
            aggregated = aggregate_router_bundle(
                bundle_dir, site_dir=site_dir, manifest_entry=manifest.get(name)
            )
            if aggregated is None:
                skipped.append({
                    "id": name,
                    "kind": "router",
                    "reason": "rows did not aggregate into a Gateway Tax report",
                })
                continue
            bundles.append(aggregated)

    bundles.sort(key=lambda b: (-leaderboard._date_key(b.get("date")), b.get("id") or ""))
    skipped.sort(key=lambda s: s.get("id") or "")
    return {
        "note": ROUTER_NOTE,
        "bundle_count": len(bundles),
        "bundles": bundles,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------


def build_board(site_dir, community_dir=None, router_dirs=None):
    """Build the combined two-family board document."""
    site_dir = os.path.abspath(site_dir)
    harness = build_harness_family(site_dir, community_dir=community_dir)
    router = build_router_family(site_dir, router_dirs=router_dirs)
    releases = leaderboard._load_manifest_list(os.path.join(site_dir, "releases.json"))
    community = leaderboard._load_manifest_list(os.path.join(site_dir, "community.json"))
    packs = leaderboard._load_manifest_list(os.path.join(site_dir, "packs.json"))
    return {
        "generated_by": "obench site",
        "schema_version": SCHEMA_VERSION,
        "cross_family_note": CROSS_FAMILY_NOTE,
        "harness": harness,
        "router": router,
        "releases": [e for e in releases if isinstance(e, dict)],
        "community": [e for e in community if isinstance(e, dict)],
        "packs": [e for e in packs if isinstance(e, dict)],
    }


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#12181f; --muted:#5b6875;
  --line:#e2e7ee; --head:#eef2f7; --accent:#0b6bcb; --accent-ink:#ffffff;
  --good:#0f7b4f; --bad:#b4442e; --warn:#8a5a00; --warn-bg:#fff4dd;
  --bar:#c8d6e6; --bar-fill:#0b6bcb; --chip:#eef2f7;
  --radius:12px; --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0d1117; --panel:#151b23; --ink:#e6edf3; --muted:#9aa7b4;
    --line:#232c37; --head:#1b232d; --accent:#5aa8ff; --accent-ink:#08121d;
    --good:#4ec98a; --bad:#ff8a70; --warn:#e8b552; --warn-bg:#2a2113;
    --bar:#2a3440; --bar-fill:#5aa8ff; --chip:#1b232d;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
  }
}
:root[data-theme="light"]{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#12181f; --muted:#5b6875;
  --line:#e2e7ee; --head:#eef2f7; --accent:#0b6bcb; --accent-ink:#ffffff;
  --good:#0f7b4f; --bad:#b4442e; --warn:#8a5a00; --warn-bg:#fff4dd;
  --bar:#c8d6e6; --bar-fill:#0b6bcb; --chip:#eef2f7;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);
}
:root[data-theme="dark"]{
  --bg:#0d1117; --panel:#151b23; --ink:#e6edf3; --muted:#9aa7b4;
  --line:#232c37; --head:#1b232d; --accent:#5aa8ff; --accent-ink:#08121d;
  --good:#4ec98a; --bad:#ff8a70; --warn:#e8b552; --warn-bg:#2a2113;
  --bar:#2a3440; --bar-fill:#5aa8ff; --chip:#1b232d;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent)}
.wrap{max-width:1240px;margin:0 auto;padding:0 20px}
header.top{border-bottom:1px solid var(--line);background:var(--panel);
  position:sticky;top:0;z-index:20}
.top .wrap{display:flex;align-items:center;gap:18px;height:60px}
.brand{font-weight:700;letter-spacing:-.01em;font-size:17px;white-space:nowrap}
.brand span{color:var(--muted);font-weight:500}
nav.tabs{display:flex;gap:2px;margin-left:auto;flex-wrap:wrap}
nav.tabs a{padding:7px 13px;border-radius:999px;text-decoration:none;color:var(--muted);
  font-weight:600;font-size:14px}
nav.tabs a[aria-current="page"]{background:var(--accent);color:var(--accent-ink)}
nav.tabs a:hover:not([aria-current]){background:var(--chip);color:var(--ink)}
button.theme{background:none;border:1px solid var(--line);color:var(--muted);
  border-radius:8px;width:34px;height:32px;cursor:pointer;padding:0;
  display:inline-flex;align-items:center;justify-content:center}
button.theme:hover{color:var(--ink);border-color:var(--muted)}
button.theme svg{width:16px;height:16px}
.hero{padding:34px 0 10px}
.hero h1{margin:0 0 6px;font-size:30px;letter-spacing:-.02em}
.hero p{margin:0;color:var(--muted);max-width:70ch}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0 4px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:12px 16px;min-width:150px}
.stat b{display:block;font-size:22px;letter-spacing:-.01em}
.stat span{color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.04em}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:12px;margin:18px 0}
.controls input[type=search],.controls select{background:var(--bg);color:var(--ink);
  border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;font-size:14px}
.controls input[type=search]{min-width:210px;flex:1}
.controls label{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:14px}
.board{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);margin:16px 0;overflow:hidden}
.board > .head{padding:16px 18px;border-bottom:1px solid var(--line)}
.board h2{margin:0 0 6px;font-size:18px;letter-spacing:-.01em}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chip{background:var(--chip);color:var(--muted);border-radius:999px;padding:3px 9px;
  font-size:12px;font-weight:600;white-space:nowrap}
.chip.kind-release{color:var(--accent)}
.chip.kind-community{color:var(--good)}
.chip.warn{background:var(--warn-bg);color:var(--warn)}
.chip.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:500}
td .chips{flex-wrap:nowrap;justify-content:flex-end}
details.caveats{margin:10px 0 0;font-size:14px}
details.caveats summary{cursor:pointer;color:var(--warn);font-weight:600}
details.caveats ul{margin:8px 0 0;padding-left:20px;color:var(--muted)}
details.caveats li{margin:5px 0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:10px 12px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th:nth-child(2),td:nth-child(2){text-align:left}
thead th{background:var(--head);font-size:12.5px;text-transform:uppercase;
  letter-spacing:.04em;color:var(--muted);position:sticky;top:0}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:var(--ink)}
thead th .arrow{opacity:.35;margin-left:4px}
thead th[aria-sort] .arrow{opacity:1;color:var(--accent)}
tbody tr:hover{background:var(--head)}
tbody tr:last-child td{border-bottom:none}
td.rank{color:var(--muted);font-weight:700;width:1%}
tr.top td.rank{color:var(--accent)}
td.name{font-weight:600}
td.sub{color:var(--muted);font-weight:400}
.ci{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.ci .track{position:relative;width:120px;height:7px;background:var(--bar);border-radius:4px}
.ci .span{position:absolute;top:0;height:7px;background:var(--bar-fill);opacity:.35;border-radius:4px}
.ci .dot{position:absolute;top:-2px;width:3px;height:11px;background:var(--bar-fill);border-radius:2px}
.ci .val{min-width:52px;text-align:right;font-weight:600}
.ci .range{color:var(--muted);font-size:12.5px;min-width:96px;text-align:right}
.delta.up{color:var(--good)}
.delta.down{color:var(--bad)}
.empty{padding:34px 18px;text-align:center;color:var(--muted)}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:var(--radius);padding:14px 16px;margin:16px 0;color:var(--muted);font-size:14.5px}
.note strong{color:var(--ink)}
.prose{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px 22px;margin:16px 0}
.prose h2{margin:0 0 8px;font-size:18px}
.prose h3{margin:20px 0 6px;font-size:15px}
.prose p,.prose li{color:var(--muted)}
.prose code{background:var(--chip);padding:1px 5px;border-radius:5px;font-size:13px}
ul.releases{list-style:none;margin:0;padding:0}
ul.releases li{padding:12px 0;border-bottom:1px solid var(--line)}
ul.releases li:last-child{border-bottom:none}
ul.releases a{font-weight:600;text-decoration:none}
ul.releases a:hover{text-decoration:underline}
footer{color:var(--muted);font-size:13.5px;padding:28px 0 40px;text-align:center}
.hidden{display:none}
@media(max-width:760px){
  .top .wrap{height:auto;padding-top:10px;padding-bottom:10px;flex-wrap:wrap}
  nav.tabs{margin-left:0;width:100%}
  .ci .track{width:64px}
  .ci .range{display:none}
  .hero h1{font-size:24px}
}
"""

_JS = r"""
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("board-data").textContent);
  var root = document.documentElement;

  // ---- theme -------------------------------------------------------------
  try {
    var saved = localStorage.getItem("obench-theme");
    if (saved) root.setAttribute("data-theme", saved);
  } catch (e) { /* storage disabled */ }
  document.getElementById("theme").addEventListener("click", function () {
    var now = root.getAttribute("data-theme");
    if (!now) {
      now = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    var next = now === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("obench-theme", next); } catch (e) { /* ignore */ }
  });

  // ---- formatting --------------------------------------------------------
  function pct(v, digits) {
    if (v === null || v === undefined) return "—";
    return (v * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  }
  function num(v, digits) {
    if (v === null || v === undefined) return "—";
    return v.toLocaleString(undefined, {
      minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0
    });
  }
  function secs(v) { return v === null || v === undefined ? "—" : v.toFixed(1) + "s"; }
  function money(v, digits) {
    if (v === null || v === undefined) return "—";
    return "$" + v.toFixed(digits === undefined ? 3 : digits);
  }
  function signed(v, fmt) {
    if (v === null || v === undefined) return "—";
    return (v > 0 ? "+" : "") + fmt(v);
  }
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k === "html") node.innerHTML = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (kid) {
      if (kid) node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
    return node;
  }
  function chip(text, cls) { return el("span", { class: "chip " + (cls || ""), text: text }); }

  // Confidence-interval cell: value, a scaled bar, and the numeric range.
  function ciCell(estimate, low, high, fmt) {
    var wrap = el("div", { class: "ci" });
    wrap.appendChild(el("span", { class: "val", text: fmt(estimate) }));
    var track = el("div", { class: "track" });
    if (low !== null && low !== undefined && high !== null && high !== undefined) {
      var lo = Math.max(0, Math.min(1, low));
      var hi = Math.max(0, Math.min(1, high));
      var span = el("div", { class: "span" });
      span.style.left = (lo * 100) + "%";
      span.style.width = Math.max(1, (hi - lo) * 100) + "%";
      track.appendChild(span);
    }
    if (estimate !== null && estimate !== undefined) {
      var dot = el("div", { class: "dot" });
      dot.style.left = "calc(" + (Math.max(0, Math.min(1, estimate)) * 100) + "% - 1px)";
      track.appendChild(dot);
    }
    wrap.appendChild(track);
    var range = "—";
    if (low !== null && low !== undefined && high !== null && high !== undefined) {
      range = fmt(low) + "–" + fmt(high);
    }
    wrap.appendChild(el("span", { class: "range", text: range }));
    return wrap;
  }

  // ---- sortable table ----------------------------------------------------
  // columns: [{label, cell(row)->Node|string, sort(row)->number|string|null,
  //            defaultDir, align}]
  var sortState = {};
  function renderTable(key, columns, rows, defaultSort) {
    var state = sortState[key] || (sortState[key] = {
      index: defaultSort === undefined ? null : defaultSort, dir: "desc"
    });
    var body = rows.slice();
    if (state.index !== null && columns[state.index] && columns[state.index].sort) {
      var get = columns[state.index].sort;
      var dir = state.dir === "asc" ? 1 : -1;
      body.sort(function (a, b) {
        var x = get(a), y = get(b);
        var xn = x === null || x === undefined, yn = y === null || y === undefined;
        if (xn && yn) return 0;
        if (xn) return 1;          // nulls always sink
        if (yn) return -1;
        if (typeof x === "string" || typeof y === "string") {
          return String(x).localeCompare(String(y)) * dir;
        }
        return (x - y) * dir;
      });
    }

    var thead = el("thead");
    var hrow = el("tr");
    columns.forEach(function (col, i) {
      var attrs = { class: col.sort ? "sortable" : "" };
      if (state.index === i) attrs["aria-sort"] = state.dir === "asc" ? "ascending" : "descending";
      var th = el("th", attrs, [col.label]);
      if (col.sort) {
        th.appendChild(el("span", {
          class: "arrow",
          text: state.index === i ? (state.dir === "asc" ? "↑" : "↓") : "↕"
        }));
        th.addEventListener("click", function () {
          if (state.index === i) state.dir = state.dir === "asc" ? "desc" : "asc";
          else { state.index = i; state.dir = col.defaultDir || "desc"; }
          render();
        });
      }
      hrow.appendChild(th);
    });
    thead.appendChild(hrow);

    var tbody = el("tbody");
    body.forEach(function (row, i) {
      var tr = el("tr", i === 0 && state.index === defaultSort ? { class: "top" } : null);
      columns.forEach(function (col) {
        var value = col.cell(row, i);
        var td = el("td", { class: col.cls || "" });
        td.appendChild(typeof value === "string" ? document.createTextNode(value) : value);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    return el("div", { class: "scroll" }, [el("table", null, [thead, tbody])]);
  }

  // ---- filters -----------------------------------------------------------
  var filters = { q: "", model: "", harness: "", hideCaveats: false };

  function matchesArm(arm) {
    var hay = ((arm.harness || arm.arm_id || "") + " " + (arm.model || arm.requested_model || "")).toLowerCase();
    if (filters.q && hay.indexOf(filters.q) === -1) return false;
    if (filters.model && (arm.model || arm.requested_model) !== filters.model) return false;
    if (filters.harness && arm.harness && arm.harness !== filters.harness) return false;
    return true;
  }

  function buildControls(models, harnesses, onChange) {
    var box = el("div", { class: "controls" });
    var search = el("input", {
      type: "search", placeholder: "Filter by harness or model…", value: filters.q
    });
    search.addEventListener("input", function () {
      filters.q = search.value.trim().toLowerCase(); onChange();
    });
    box.appendChild(search);

    function select(label, values, current, apply) {
      var sel = el("select");
      sel.appendChild(el("option", { value: "", text: label }));
      values.forEach(function (v) {
        var opt = el("option", { value: v, text: v });
        if (v === current) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", function () { apply(sel.value); onChange(); });
      return sel;
    }
    if (models.length) {
      box.appendChild(select("All models", models, filters.model, function (v) { filters.model = v; }));
    }
    if (harnesses.length) {
      box.appendChild(select("All harnesses", harnesses, filters.harness, function (v) { filters.harness = v; }));
    }
    var cb = el("input", { type: "checkbox" });
    cb.checked = filters.hideCaveats;
    cb.addEventListener("change", function () { filters.hideCaveats = cb.checked; onChange(); });
    box.appendChild(el("label", null, [cb, "Hide boards with disclosed caveats"]));
    return box;
  }

  // ---- harness view ------------------------------------------------------
  function harnessBoard(bundle) {
    var arms = bundle.arms.filter(matchesArm);
    if (!arms.length) return null;

    var head = el("div", { class: "head" });
    var title = bundle.path
      ? el("a", { href: bundle.path, text: bundle.title })
      : document.createTextNode(bundle.title);
    head.appendChild(el("h2", null, [title]));

    var chips = el("div", { class: "chips" }, [
      chip(bundle.kind, "kind-" + bundle.kind),
      bundle.date ? chip(bundle.date) : null,
      chip(bundle.table === "matched"
        ? "matched denominators" : "all countable cells"),
      chip(bundle.countable_rows + " countable cells"),
      bundle.results_sha256 ? chip("results " + bundle.results_sha256.slice(0, 12), "mono") : null
    ]);
    (bundle.models || []).forEach(function (m) { chips.appendChild(chip(m)); });
    if (bundle.has_caveats) chips.appendChild(chip("caveats disclosed", "warn"));
    head.appendChild(chips);

    if (bundle.has_caveats) {
      var det = el("details", { class: "caveats" }, [
        el("summary", { text: bundle.caveats.length + " caveat(s) from the release page" })
      ]);
      var ul = el("ul");
      bundle.caveats.forEach(function (c) { ul.appendChild(el("li", { text: c })); });
      det.appendChild(ul);
      head.appendChild(det);
    }

    var columns = [
      { label: "#", cls: "rank", cell: function (r, i) { return String(i + 1); } },
      {
        label: "Harness", cls: "name",
        cell: function (r) { return r.harness; },
        sort: function (r) { return r.harness; }, defaultDir: "asc"
      },
      {
        label: "Model", cls: "sub",
        cell: function (r) { return r.model; },
        sort: function (r) { return r.model; }, defaultDir: "asc"
      },
      {
        label: "Solve rate (Wilson 95%)",
        cell: function (r) {
          return ciCell(r.solve_rate, r.wilson95 && r.wilson95[0], r.wilson95 && r.wilson95[1],
            function (v) { return pct(v); });
        },
        sort: function (r) { return r.solve_rate; }
      },
      {
        label: "Solved",
        cell: function (r) { return r.solved + "/" + r.n; },
        sort: function (r) { return r.n ? r.solved / r.n : null; }
      },
      {
        label: "Median wall",
        cell: function (r) { return secs(r.median_wall_s); },
        sort: function (r) { return r.median_wall_s; }, defaultDir: "asc"
      },
      {
        label: "Tokens/solve",
        cell: function (r) { return num(r.total_tokens_per_solve); },
        sort: function (r) { return r.total_tokens_per_solve; }, defaultDir: "asc"
      },
      {
        label: "$/solve",
        cell: function (r) { return money(r.cost_per_solve_usd); },
        sort: function (r) { return r.cost_per_solve_usd; }, defaultDir: "asc"
      },
      {
        label: "Token basis", cls: "sub",
        cell: function (r) {
          var box = el("div", { class: "chips" });
          (r.token_bases || []).forEach(function (b) { box.appendChild(chip(b)); });
          if (!(r.token_bases || []).length) box.appendChild(chip("unknown"));
          return box;
        }
      }
    ];

    var foot = el("div", { class: "head" }, [
      el("span", { class: "chips" }, [
        bundle.results_path ? el("a", { href: bundle.results_path, text: "results.jsonl" }) : null,
        bundle.task_set_digest ? chip("taskset " + bundle.task_set_digest.slice(0, 12), "mono") : null
      ])
    ]);

    return el("section", { class: "board" }, [
      head, renderTable("h:" + bundle.id, columns, arms, 3), foot
    ]);
  }

  function renderHarness(host) {
    host.innerHTML = "";
    var fam = DATA.harness;
    var models = {}, harnesses = {};
    fam.bundles.forEach(function (b) {
      b.arms.forEach(function (a) { models[a.model] = 1; harnesses[a.harness] = 1; });
    });
    host.appendChild(buildControls(
      Object.keys(models).sort(), Object.keys(harnesses).sort(), render
    ));
    host.appendChild(el("div", { class: "note" }, [
      el("strong", { text: "How to read this. " }), fam.note
    ]));

    var shown = 0;
    fam.bundles.forEach(function (b) {
      if (filters.hideCaveats && b.has_caveats) return;
      var board = harnessBoard(b);
      if (board) { host.appendChild(board); shown++; }
    });
    if (!shown) {
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "empty", text: "No boards match the current filters." })
      ]));
    }
    if ((fam.skipped || []).length) {
      var ul = el("ul", { class: "releases" });
      fam.skipped.forEach(function (s) {
        ul.appendChild(el("li", null, [
          el("strong", { text: s.id }), " — " + s.reason
        ]));
      });
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "head" }, [
          el("h2", { text: "Not ranked (" + fam.skipped.length + ")" }),
          el("p", { class: "sub", text: "Published pages without machine-verifiable results." })
        ]),
        el("div", { class: "head" }, [ul])
      ]));
    }
  }

  // ---- router view -------------------------------------------------------
  function routerBoard(bundle) {
    var head = el("div", { class: "head" });
    var title = bundle.path
      ? el("a", { href: bundle.path, text: bundle.title })
      : document.createTextNode(bundle.title);
    head.appendChild(el("h2", null, [title]));
    var chips = el("div", { class: "chips" }, [
      chip(bundle.track || "gateway_tax"),
      bundle.harness ? chip("harness " + bundle.harness) : null,
      bundle.date ? chip(bundle.date) : null,
      chip(bundle.blocks_included + "/" + bundle.blocks_observed + " blocks included"),
      chip(bundle.tasks_included + " tasks"),
      bundle.execution_lane ? chip(bundle.execution_lane) : null,
      bundle.experiment_digest ? chip("exp " + bundle.experiment_digest.slice(0, 12), "mono") : null
    ]);
    Object.keys(bundle.blocks_excluded || {}).forEach(function (reason) {
      chips.appendChild(chip("excluded " + reason + "=" + bundle.blocks_excluded[reason], "warn"));
    });
    head.appendChild(chips);

    var armCols = [
      { label: "#", cls: "rank", cell: function (r, i) { return String(i + 1); } },
      {
        label: "Route", cls: "name",
        cell: function (r) { return r.arm_id; },
        sort: function (r) { return r.arm_id; }, defaultDir: "asc"
      },
      {
        label: "Role", cls: "sub",
        cell: function (r) {
          return el("div", { class: "chips" }, [
            chip(r.role || "—", r.role === "direct" ? "kind-release" : ""),
            r.requested_provider ? chip(r.requested_provider) : null
          ]);
        },
        sort: function (r) { return r.role; }, defaultDir: "asc"
      },
      {
        label: "Solve rate (95% CI)",
        cell: function (r) {
          return ciCell(r.solve_rate.estimate, r.solve_rate.low, r.solve_rate.high,
            function (v) { return pct(v); });
        },
        sort: function (r) { return r.solve_rate.estimate; }
      },
      {
        label: "Mean score",
        cell: function (r) {
          var m = r.mean_checker_score.estimate;
          return m === null || m === undefined ? "—" : m.toFixed(3);
        },
        sort: function (r) { return r.mean_checker_score.estimate; }
      },
      {
        label: "Availability",
        cell: function (r) { return pct(r.availability.estimate); },
        sort: function (r) { return r.availability.estimate; }
      },
      {
        label: "Latency",
        cell: function (r) { return secs(r.latency_s.estimate); },
        sort: function (r) { return r.latency_s.estimate; }, defaultDir: "asc"
      },
      {
        label: "$/solve",
        cell: function (r) { return r.cost ? money(r.cost.cost_per_solve_usd, 4) : "—"; },
        sort: function (r) { return r.cost ? r.cost.cost_per_solve_usd : null; },
        defaultDir: "asc"
      },
      {
        label: "Cost basis", cls: "sub",
        cell: function (r) {
          if (!r.cost) return "—";
          var box = el("div", { class: "chips" }, [chip(r.cost.basis)]);
          if (r.cost.coverage_ratio !== null && r.cost.coverage_ratio !== undefined
              && r.cost.coverage_ratio < 1) {
            box.appendChild(chip(pct(r.cost.coverage_ratio, 0) + " coverage", "warn"));
          }
          return box;
        }
      }
    ];

    var parts = [head, renderTable("r:" + bundle.id, armCols, bundle.arms, 3)];

    if ((bundle.contrasts || []).length) {
      var taxCols = [
        {
          label: "Gateway arm", cls: "name",
          cell: function (r) { return r.arm_id; },
          sort: function (r) { return r.arm_id; }, defaultDir: "asc"
        },
        {
          label: "vs direct", cls: "sub",
          cell: function (r) { return r.direct_arm; }
        },
        {
          label: "Δ solve rate",
          cell: function (r) { return deltaCell(r.solve_rate, function (v) { return pct(v); }, true); },
          sort: function (r) { return r.solve_rate.estimate; }
        },
        {
          label: "Δ mean score",
          cell: function (r) {
            return deltaCell(r.mean_checker_score, function (v) { return v.toFixed(3); }, true);
          },
          sort: function (r) { return r.mean_checker_score.estimate; }
        },
        {
          label: "Δ availability",
          cell: function (r) { return deltaCell(r.availability, function (v) { return pct(v); }, true); },
          sort: function (r) { return r.availability.estimate; }
        },
        {
          label: "Δ latency",
          cell: function (r) {
            return deltaCell(r.latency_s, function (v) { return v.toFixed(2) + "s"; }, false);
          },
          sort: function (r) { return r.latency_s.estimate; }, defaultDir: "asc"
        }
      ];
      parts.push(el("div", { class: "head" }, [
        el("h2", { text: "Gateway tax" }),
        el("p", {
          class: "sub",
          text: "Paired, task-weighted difference from the direct control arm, with "
            + "bootstrap 95% intervals. An interval spanning zero is not a detected effect."
        })
      ]));
      parts.push(renderTable("t:" + bundle.id, taxCols, bundle.contrasts, 2));
    }

    return el("section", { class: "board" }, parts);
  }

  // Signed delta with its interval; `higherIsBetter` colours the estimate.
  function deltaCell(metric, fmt, higherIsBetter) {
    var v = metric.estimate;
    var cls = "";
    if (v !== null && v !== undefined && v !== 0) {
      var good = higherIsBetter ? v > 0 : v < 0;
      cls = good ? "delta up" : "delta down";
    }
    var wrap = el("div", { class: "ci" }, [
      el("span", { class: "val " + cls, text: signed(v, fmt) })
    ]);
    var range = "—";
    if (metric.low !== null && metric.low !== undefined
        && metric.high !== null && metric.high !== undefined) {
      range = signed(metric.low, fmt) + " – " + signed(metric.high, fmt);
    }
    wrap.appendChild(el("span", { class: "range", text: range }));
    return wrap;
  }

  function renderRouter(host) {
    host.innerHTML = "";
    var fam = DATA.router;
    host.appendChild(el("div", { class: "note" }, [
      el("strong", { text: "How to read this. " }), fam.note
    ]));
    if (!fam.bundles.length) {
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "empty" }, [
          el("p", { text: "No verified Router Bench bundles are published yet." }),
          el("p", {
            html: "Produce one with <code>obench router run</code> then "
              + "<code>obench router publish &lt;results&gt; &lt;experiment&gt; "
              + "docs/router/&lt;id&gt;</code>, and re-run <code>obench site build</code>."
          })
        ])
      ]));
    }
    fam.bundles.forEach(function (b) { host.appendChild(routerBoard(b)); });
    (fam.skipped || []).forEach(function (s) {
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "empty", text: s.id + " — " + s.reason })
      ]));
    });
  }

  // ---- releases view -----------------------------------------------------
  function renderReleases(host) {
    host.innerHTML = "";
    function list(title, entries, blurb) {
      if (!entries.length) return;
      var ul = el("ul", { class: "releases" });
      entries.forEach(function (e) {
        var line = el("li");
        line.appendChild(e.path
          ? el("a", { href: e.path, text: e.title || e.id })
          : el("strong", { text: e.title || e.id }));
        var meta = [e.date, (e.models || []).join(", "), e.submitter].filter(Boolean).join(" · ");
        if (meta) line.appendChild(el("div", { class: "sub", text: meta }));
        if (e.description) line.appendChild(el("div", { class: "sub", text: e.description }));
        ul.appendChild(line);
      });
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "head" }, [
          el("h2", { text: title }),
          blurb ? el("p", { class: "sub", text: blurb }) : null
        ]),
        el("div", { class: "head" }, [ul])
      ]));
    }
    list("Releases", DATA.releases, "First-party published comparison bundles.");
    list("Community", DATA.community,
      "Third-party bundles re-verified by CI. Digests prove tamper-evidence, not that runs were not cherry-picked.");
    list("Packs", DATA.packs, "Versioned task and harness packs.");
  }

  // ---- routing -----------------------------------------------------------
  var VIEWS = {
    harness: renderHarness,
    router: renderRouter,
    releases: renderReleases,
    methodology: null
  };

  function currentView() {
    var hash = (location.hash || "").replace("#", "");
    return VIEWS.hasOwnProperty(hash) ? hash : "harness";
  }

  function render() {
    var view = currentView();
    Object.keys(VIEWS).forEach(function (name) {
      var host = document.getElementById("view-" + name);
      host.classList.toggle("hidden", name !== view);
    });
    document.querySelectorAll("nav.tabs a").forEach(function (a) {
      if (a.getAttribute("href") === "#" + view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    if (VIEWS[view]) VIEWS[view](document.getElementById("view-" + view));
  }

  window.addEventListener("hashchange", render);
  render();
})();
"""


def _headline_stats(doc):
    harness = doc["harness"]
    router = doc["router"]
    arms = sum(len(b["arms"]) for b in harness["bundles"])
    harnesses = {a["harness"] for b in harness["bundles"] for a in b["arms"]}
    models = {a["model"] for b in harness["bundles"] for a in b["arms"]}
    routes = sum(len(b["arms"]) for b in router["bundles"])
    return [
        (str(harness["bundle_count"]), "verified harness bundles"),
        (str(len(harnesses)), "harnesses ranked"),
        (str(len(models)), "models covered"),
        (str(arms), "harness arms"),
        (str(router["bundle_count"]), "router bundles"),
        (str(routes), "serving routes"),
    ]


_METHODOLOGY = """
<section class="prose">
  <h2>What is being measured</h2>
  <p>OpenBench runs two benchmark families. They share a task contract and a
  checker, and nothing else. Never compare a number across them.</p>

  <h3>Harness Bench</h3>
  <p>Varies the coding-agent harness — the CLI that wraps a model in a run loop,
  tool set, and permission policy — while holding the model and task fixed. An
  arm is <code>(harness, model)</code>. A task is solved when its
  <code>checker.sh</code> exits 0; the harness's own claim of success is never
  trusted.</p>

  <h3>Router Bench</h3>
  <p>Holds the harness, model, provider, sampling, task, and budget fixed while
  varying the serving route. An arm is a route. The implemented track is
  <strong>Gateway Tax</strong>: a direct baseline against one or more gateway
  arms on the same canonical model revision, with fallbacks, gateway retries,
  and caching disabled, so the measurement is the gateway path itself rather
  than its model choice or cache hit rate.</p>

  <h2>Denominators and intervals</h2>
  <ul>
    <li>Denominators are countable cells. Infrastructure and rate-limit failures
    are excluded; other failures, including timeouts, stay in the denominator.</li>
    <li>Harness Bench uses Wilson 95% intervals over matched
    <code>(task, trial)</code> cells whenever a bundle has two or more arms.</li>
    <li>Router Bench uses task-weighted estimates with bootstrap 95% intervals,
    and includes a block only when every expected arm is present,
    infrastructure-valid, and passes route integrity.</li>
    <li>A gateway-tax interval that spans zero is not a detected effect.</li>
  </ul>

  <h2>Efficiency and cost</h2>
  <ul>
    <li>Median wall time is taken among solved cells only.</li>
    <li>Tokens per solve use split fields — uncached input, output, and cache
    reads — never a vendor aggregate. The token basis chip records whether a
    figure was proxy-metered, vendor-split, or self-reported by the CLI. These
    bases are not interchangeable.</li>
    <li>Harness <code>$/solve</code> appears only for models with a configured
    price. Router cost prefers invoice reconciliation, then the router's own
    reported cost, then a frozen list-price estimate, and shows coverage when a
    basis does not cover every call.</li>
    <li>Harness defaults are deliberately not clamped: they are part of the
    product being evaluated.</li>
  </ul>

  <h2>Comparability</h2>
  <ul>
    <li>Cells from different bundles are never blended. Each board is one
    bundle; cross-bundle ranking on different task sets is not supported.</li>
    <li>Every ranked bundle ships <code>results.jsonl</code> plus a provenance
    digest and is re-verified before it appears here. Digests prove
    tamper-evidence, not that runs were not cherry-picked — which is why
    disclosed caveats are shown next to the scores rather than in a footnote.</li>
    <li>Results cover only the included tasks, trials, model deployments,
    harness versions, and timeout caps. They do not establish a universal
    ranking.</li>
  </ul>

  <h2>Reproducing a board</h2>
  <p>Every board links its <code>results.jsonl</code>. Re-check a bundle with
  <code>obench verify &lt;bundle&gt;</code> (harness) or
  <code>obench router verify &lt;bundle&gt;</code> (router), and rebuild this
  page with <code>obench site build</code>.</p>
</section>
"""


def render_board_html(doc):
    """Render the self-contained board page for a built document."""
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # `</script>` inside JSON would close the host element early.
    payload = payload.replace("</", "<\\/")

    stat_html = "".join(
        f'<div class="stat"><b>{html.escape(value)}</b>'
        f'<span>{html.escape(label)}</span></div>'
        for value, label in _headline_stats(doc)
    )

    tabs = (
        '<a href="#harness">Harness Bench</a>'
        '<a href="#router">Router Bench</a>'
        '<a href="#releases">Releases</a>'
        '<a href="#methodology">Methodology</a>'
    )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>OpenBench leaderboards</title>"
        '<meta name="description" content="Harness and serving-route leaderboards '
        'for OpenBench, built from digest-verified result bundles.">'
        f"<style>{_CSS}</style></head><body>"
        '<header class="top"><div class="wrap">'
        '<div class="brand">OpenBench <span>leaderboards</span></div>'
        f'<nav class="tabs">{tabs}</nav>'
        '<button class="theme" id="theme" type="button" '
        'aria-label="Toggle colour theme" title="Toggle colour theme">'
        '<svg viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">'
        '<path d="M8 0a8 8 0 1 0 0 16A8 8 0 0 0 8 0zm0 1.5V14.5a6.5 6.5 0 0 1 0-13z"/>'
        "</svg></button>"
        "</div></header>"
        '<div class="wrap">'
        '<div class="hero"><h1>Same task, different wrapper — and different wire.</h1>'
        f"<p>{html.escape(doc['cross_family_note'])}</p>"
        f'<div class="stats">{stat_html}</div></div>'
        "<noscript><p class=\"note\">This page renders its tables with JavaScript. "
        'The static fallback is <a href="leaderboard.html">leaderboard.html</a>, '
        'and the same data is in <a href="board.json">board.json</a>.</p></noscript>'
        '<main id="view-harness"></main>'
        '<main id="view-router" class="hidden"></main>'
        '<main id="view-releases" class="hidden"></main>'
        f'<main id="view-methodology" class="hidden">{_METHODOLOGY}</main>'
        "<footer>Generated by <code>obench site</code> · static, self-contained, "
        'no third-party assets · <a href="board.json">board.json</a> · '
        '<a href="index.html">all releases</a></footer>'
        "</div>"
        f'<script id="board-data" type="application/json">{payload}</script>'
        f"<script>{_JS}</script>"
        "</body></html>\n"
    )


def write_board(site_dir, community_dir=None, router_dirs=None):
    """Build and write ``board.json`` + ``board.html`` under ``site_dir``."""
    site_dir = os.path.abspath(site_dir)
    doc = build_board(site_dir, community_dir=community_dir, router_dirs=router_dirs)
    json_path = os.path.join(site_dir, "board.json")
    html_path = os.path.join(site_dir, "board.html")
    leaderboard._write_text(
        json_path,
        json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    leaderboard._write_text(html_path, render_board_html(doc))
    return {
        "json_path": json_path,
        "html_path": html_path,
        "harness_bundles": doc["harness"]["bundle_count"],
        "router_bundles": doc["router"]["bundle_count"],
        "skipped": len(doc["harness"]["skipped"]) + len(doc["router"]["skipped"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench site",
        description="Build the unified static leaderboard site (harness + router).",
    )
    sub = parser.add_subparsers(dest="command")
    build = sub.add_parser("build", help="write board.html + board.json")
    build.add_argument(
        "--site-dir",
        default=leaderboard._default_site_dir(),
        help="GitHub Pages root (default: docs/)",
    )
    build.add_argument(
        "--community-dir",
        default=None,
        help="optional data/community root to include (default: auto when present)",
    )
    build.add_argument(
        "--no-community-dir",
        action="store_true",
        help="do not scan data/community",
    )
    build.add_argument(
        "--router-dir",
        action="append",
        default=None,
        help="router bundle root (repeatable; default: <site-dir>/router)",
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "build":
        community_dir = args.community_dir
        if community_dir is None and not args.no_community_dir:
            community_dir = leaderboard._default_community_dir()
        if args.no_community_dir:
            community_dir = None
        info = write_board(
            args.site_dir,
            community_dir=community_dir,
            router_dirs=args.router_dir,
        )
        print(f"board.html  {info['html_path']}")
        print(f"board.json  {info['json_path']}")
        print(
            f"harness_bundles={info['harness_bundles']} "
            f"router_bundles={info['router_bundles']} "
            f"skipped={info['skipped']}"
        )
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
