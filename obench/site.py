#!/usr/bin/env python3
"""Unified static leaderboard site for both OpenBench benchmark families.

``obench site build`` scans the GitHub Pages root and emits two artifacts:

* ``board.json`` — one machine-readable document covering **Harness Bench**
  (verified ``results.jsonl`` publish bundles, aggregated by
  :mod:`obench.leaderboard`) and **Router Bench** (verified ``router_bench``
  evidence bundles, aggregated by :mod:`obench.router_report`).
* ``index.html`` — the site's landing page, which *is* the leaderboard: family
  tabs, per-board sortable tables, model/harness filters, Wilson and bootstrap
  confidence intervals drawn as bars, and the Gateway Tax contrast table.

Every table is rendered here, in Python, at build time. The page's script only
enhances what is already in the document — it re-orders rows, hides them, and
switches tabs — so the page is complete with JavaScript switched off and there
is exactly one renderer to keep honest. No server, no build step, and no
third-party assets, the same constraints as every other page this repo
publishes.

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

# Short column labels; the full basis name stays available on hover.
COST_BASIS_LABELS = {
    "invoice_reconciled": "invoice",
    "router_reported": "router",
    "frozen_list_estimate": "list est.",
}


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
/* ---------------------------------------------------------------------------
   OpenBench leaderboards — instrument readout.

   Type: prose in sans; every identifier the benchmark names and every value it
   measures in mono, because they are readings, not writing.
   Colour: cool-slate neutrals biased toward the accent; one accent (validated
   categorical slot 1) for interactive chrome and interval marks; a validated
   blue/orange diverging pair for signed contrasts, with a neutral midpoint when
   an interval spans zero; amber reserved for provenance warnings and never
   carrying meaning without a label.
--------------------------------------------------------------------------- */
:root{
  color-scheme:light;
  --font-sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --font-mono:ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono","Cascadia Mono",
    Menlo,Consolas,"Liberation Mono",monospace;

  --surface:#eef1f5;
  --panel:#ffffff;
  --panel-2:#f6f8fb;
  --line:#dce3ea;
  --line-strong:#c3ced9;
  --ink:#0e1720;
  --ink-2:#465768;
  --ink-3:#6d7f92;

  --accent:#2a78d6;
  --accent-rgb:42,120,214;
  --pole-better:#2a78d6;
  --pole-better-rgb:42,120,214;
  --pole-worse:#eb6834;
  --pole-worse-rgb:235,104,52;
  --pole-null:#8494a5;
  --pole-null-rgb:132,148,165;

  --warn-ink:#8a5300;
  --warn-bg:#fdf3dd;
  --warn-line:#e8c98a;

  --track:rgba(14,23,32,.09);
  --grid:rgba(14,23,32,.10);
  --shadow:0 1px 1px rgba(14,23,32,.04),0 6px 18px rgba(14,23,32,.05);
  --radius:10px;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --surface:#080c11;
    --panel:#111820;
    --panel-2:#161f29;
    --line:#1e2833;
    --line-strong:#2b3846;
    --ink:#e6edf5;
    --ink-2:#9fb1c3;
    --ink-3:#74879b;

    --accent:#3987e5;
    --accent-rgb:57,135,229;
    --pole-better:#3987e5;
    --pole-better-rgb:57,135,229;
    --pole-worse:#d95926;
    --pole-worse-rgb:217,89,38;
    --pole-null:#74879b;
    --pole-null-rgb:116,135,155;

    --warn-ink:#fab219;
    --warn-bg:#2a2113;
    --warn-line:#5a4715;

    --track:rgba(230,237,245,.10);
    --grid:rgba(230,237,245,.13);
    --shadow:0 1px 1px rgba(0,0,0,.5),0 6px 18px rgba(0,0,0,.35);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface:#080c11;
  --panel:#111820;
  --panel-2:#161f29;
  --line:#1e2833;
  --line-strong:#2b3846;
  --ink:#e6edf5;
  --ink-2:#9fb1c3;
  --ink-3:#74879b;

  --accent:#3987e5;
  --accent-rgb:57,135,229;
  --pole-better:#3987e5;
  --pole-better-rgb:57,135,229;
  --pole-worse:#d95926;
  --pole-worse-rgb:217,89,38;
  --pole-null:#74879b;
  --pole-null-rgb:116,135,155;

  --warn-ink:#fab219;
  --warn-bg:#2a2113;
  --warn-line:#5a4715;

  --track:rgba(230,237,245,.10);
  --grid:rgba(230,237,245,.13);
  --shadow:0 1px 1px rgba(0,0,0,.5),0 6px 18px rgba(0,0,0,.35);
}

/* Explicit light scope: the toggle must be able to win against an OS set to
   dark, including flipping color-scheme back for native form controls. */
:root[data-theme="light"]{
  color-scheme:light;
  --surface:#eef1f5;
  --panel:#ffffff;
  --panel-2:#f6f8fb;
  --line:#dce3ea;
  --line-strong:#c3ced9;
  --ink:#0e1720;
  --ink-2:#465768;
  --ink-3:#6d7f92;

  --accent:#2a78d6;
  --accent-rgb:42,120,214;
  --pole-better:#2a78d6;
  --pole-better-rgb:42,120,214;
  --pole-worse:#eb6834;
  --pole-worse-rgb:235,104,52;
  --pole-null:#8494a5;
  --pole-null-rgb:132,148,165;

  --warn-ink:#8a5300;
  --warn-bg:#fdf3dd;
  --warn-line:#e8c98a;

  --track:rgba(14,23,32,.09);
  --grid:rgba(14,23,32,.10);
  --shadow:0 1px 1px rgba(14,23,32,.04),0 6px 18px rgba(14,23,32,.05);
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--surface);color:var(--ink);
  font:15px/1.55 var(--font-sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-underline-offset:2px}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,
summary:focus-visible,th.sortable:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms !important;transition-duration:.01ms !important}
}

.wrap{max-width:1280px;margin:0 auto;padding:0 22px}

/* --- masthead ---------------------------------------------------------- */
header.top{background:var(--panel);border-bottom:1px solid var(--line);
  position:sticky;top:0;z-index:30}
.top .wrap{display:flex;align-items:center;gap:20px;min-height:58px;flex-wrap:wrap}
.brand{display:flex;align-items:baseline;gap:8px;white-space:nowrap}
.brand .cmd{font:600 15px/1 var(--font-mono);letter-spacing:-.01em;color:var(--ink)}
.brand .cmd::before{content:"$ ";color:var(--ink-3);font-weight:400}
.brand .what{font-size:14px;color:var(--ink-2)}
nav.tabs{display:flex;gap:1px;margin-left:auto;flex-wrap:wrap}
nav.tabs a{padding:6px 12px;border-radius:7px;text-decoration:none;color:var(--ink-2);
  font-weight:600;font-size:13.5px}
nav.tabs a:hover:not([aria-current]){background:var(--panel-2);color:var(--ink)}
nav.tabs a[aria-current="page"]{background:var(--ink);color:var(--panel)}
button.theme{background:none;border:1px solid var(--line-strong);color:var(--ink-2);
  border-radius:7px;width:32px;height:30px;cursor:pointer;padding:0;
  display:inline-flex;align-items:center;justify-content:center}
button.theme:hover{color:var(--ink);border-color:var(--ink-3)}
button.theme svg{width:15px;height:15px}

/* --- page intro & summary strip ---------------------------------------- */
.intro{padding:30px 0 6px;max-width:64ch}
.intro h1{margin:0 0 8px;font-size:25px;line-height:1.25;letter-spacing:-.02em;
  text-wrap:balance}
.intro p{margin:0;color:var(--ink-2)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);
  border-radius:var(--radius);overflow:hidden;margin:22px 0 4px}
.stat{background:var(--panel);padding:12px 15px}
.stat b{display:block;font:600 21px/1.15 var(--font-mono);letter-spacing:-.02em}
.stat span{display:block;margin-top:3px;color:var(--ink-3);font-size:11px;
  text-transform:uppercase;letter-spacing:.07em;font-weight:600}

/* --- controls ----------------------------------------------------------- */
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:20px 0 14px}
.controls input[type=search],.controls select{background:var(--panel);color:var(--ink);
  border:1px solid var(--line-strong);border-radius:7px;padding:7px 10px;
  font:14px var(--font-sans)}
.controls input[type=search]{min-width:220px;flex:1;font-family:var(--font-mono);
  font-size:13px}
.controls label{display:flex;align-items:center;gap:7px;color:var(--ink-2);font-size:13.5px}

/* --- notes -------------------------------------------------------------- */
.note{background:var(--panel);border:1px solid var(--line);
  border-left:2px solid var(--accent);border-radius:var(--radius);
  padding:13px 16px;margin:14px 0;color:var(--ink-2);font-size:14px;max-width:88ch}
.note strong{color:var(--ink)}

/* --- board -------------------------------------------------------------- */
.board{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);margin:14px 0;overflow:hidden}
.board .head{padding:15px 18px;border-bottom:1px solid var(--line)}
.board .head:last-child{border-bottom:none;border-top:1px solid var(--line)}
.board h2{margin:0;font-size:17px;letter-spacing:-.015em;line-height:1.3}
.board h2 a{text-decoration:none}
.board h2 a:hover{text-decoration:underline}
.board .head p{margin:6px 0 0;color:var(--ink-2);font-size:13.5px;max-width:80ch}
.meta{margin-top:9px;font:12px/1.7 var(--font-mono);color:var(--ink-3);
  display:flex;flex-wrap:wrap;gap:0 14px}
.meta b{color:var(--ink-2);font-weight:600}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.chip{border:1px solid var(--line-strong);color:var(--ink-2);background:var(--panel-2);
  border-radius:5px;padding:2px 7px;font:600 11.5px var(--font-mono);white-space:nowrap}
.chip.warn{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn-ink)}
.chip.role-direct{border-color:rgba(var(--accent-rgb),.5);color:var(--accent)}
td .chips{flex-wrap:nowrap;justify-content:flex-end;margin-top:0}

details.caveats{margin-top:10px;font-size:13.5px}
details.caveats summary{cursor:pointer;color:var(--warn-ink);font-weight:600;
  list-style:none;display:flex;align-items:center;gap:6px}
details.caveats summary::-webkit-details-marker{display:none}
details.caveats summary::before{content:"▸";font-size:11px}
details.caveats[open] summary::before{content:"▾"}
details.caveats ul{margin:9px 0 0;padding-left:18px;color:var(--ink-2)}
details.caveats li{margin:6px 0;max-width:88ch}

/* --- tables ------------------------------------------------------------- */
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:9px 10px;text-align:right;white-space:nowrap;
  border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
thead th{background:var(--panel-2);color:var(--ink-3);font:600 11px var(--font-sans);
  text-transform:uppercase;letter-spacing:.07em;padding-top:11px;padding-bottom:11px;
  vertical-align:bottom}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:var(--ink)}
thead th .arrow{opacity:.4;margin-left:4px;font-size:10px}
thead th[aria-sort] .arrow{opacity:1;color:var(--accent)}
thead th[aria-sort]{color:var(--ink)}
tbody td{font:13.5px/1.4 var(--font-mono);font-variant-numeric:tabular-nums;
  color:var(--ink-2)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--panel-2)}
td.rank{color:var(--ink-3);width:1%;padding-right:2px}
td.name{color:var(--ink);font-weight:600}
/* Keep the arm's identity on screen while the measurements scroll. */
thead th:first-child,tbody td:first-child{position:sticky;left:0;z-index:1;
  background:var(--panel)}
thead th:first-child{background:var(--panel-2)}
tbody tr:hover td:first-child{background:var(--panel-2)}

/* --- interval plot: one shared 0–100% scale across every row ------------ */
.axis{display:flex;justify-content:space-between;width:var(--plot-w);
  margin:5px 0 0 auto;font:500 10px var(--font-mono);color:var(--ink-3);
  letter-spacing:0;text-transform:none}
.iv{display:flex;align-items:center;gap:9px;justify-content:flex-end}
.iv .val{flex:0 0 auto;min-width:54px;text-align:right;color:var(--ink);font-weight:600}
.iv .track{position:relative;flex:0 0 var(--plot-w);width:var(--plot-w);height:9px;
  background:var(--track);
  border-radius:2px;
  background-image:linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:25% 100%;background-position:0 0}
.iv .span{position:absolute;top:0;height:9px;border-radius:2px;
  background:rgba(var(--accent-rgb),.34)}
.iv .dot{position:absolute;top:-2px;width:3px;height:13px;border-radius:1.5px;
  background:var(--accent);box-shadow:0 0 0 2px var(--panel)}
.iv .range{flex:0 0 auto;min-width:76px;text-align:right;color:var(--ink-3);font-size:11px}

/* --- signed contrast: diverging, centred on a zero line ----------------- */
.dv{display:flex;align-items:center;gap:9px;justify-content:flex-end}
.dv .val{flex:0 0 auto;min-width:58px;text-align:right;font-weight:600}
.dv .val.better{color:var(--pole-better)}
.dv .val.worse{color:var(--pole-worse)}
.dv .val.null{color:var(--ink-2)}
.dv .track{position:relative;flex:0 0 var(--plot-w);width:var(--plot-w);height:9px;
  background:var(--track);
  border-radius:2px}
.dv .zero{position:absolute;left:50%;top:-3px;width:1px;height:15px;
  background:var(--line-strong)}
.dv .span{position:absolute;top:0;height:9px;border-radius:2px}
.dv .span.better{background:rgba(var(--pole-better-rgb),.34)}
.dv .span.worse{background:rgba(var(--pole-worse-rgb),.34)}
.dv .span.null{background:rgba(var(--pole-null-rgb),.30)}
.dv .dot{position:absolute;top:-2px;width:3px;height:13px;border-radius:1.5px;
  box-shadow:0 0 0 2px var(--panel)}
.dv .dot.better{background:var(--pole-better)}
.dv .dot.worse{background:var(--pole-worse)}
.dv .dot.null{background:var(--pole-null)}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;
  font:12px var(--font-sans);color:var(--ink-2)}
.legend span{display:flex;align-items:center;gap:6px}
.legend i{width:11px;height:11px;border-radius:2px;display:inline-block}
.legend i.better{background:var(--pole-better)}
.legend i.worse{background:var(--pole-worse)}
.legend i.null{background:var(--pole-null)}

/* --- lists & prose ------------------------------------------------------ */
.empty{padding:36px 18px;text-align:center;color:var(--ink-2)}
.empty p{margin:0 0 8px;max-width:60ch;margin-inline:auto}
.empty code{font-family:var(--font-mono);font-size:12.5px;background:var(--panel-2);
  border:1px solid var(--line);border-radius:4px;padding:1px 5px}
ul.records{list-style:none;margin:0;padding:0}
ul.records li{padding:11px 0;border-bottom:1px solid var(--line)}
ul.records li:last-child{border-bottom:none}
ul.records a,ul.records strong{font-weight:600;text-decoration:none}
ul.records a:hover{text-decoration:underline}
ul.records .sub{color:var(--ink-3);font:12px/1.6 var(--font-mono);margin-top:2px}
.prose{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:22px 26px;margin:14px 0;max-width:80ch}
.prose h2{margin:0 0 8px;font-size:17px;letter-spacing:-.015em}
.prose h2+p{margin-top:0}
.prose h3{margin:22px 0 6px;font-size:14px;letter-spacing:.01em}
.prose p,.prose li{color:var(--ink-2);max-width:74ch}
.prose li{margin:6px 0}
.prose code{font-family:var(--font-mono);font-size:12.5px;background:var(--panel-2);
  border:1px solid var(--line);border-radius:4px;padding:1px 5px}
footer{color:var(--ink-3);font-size:13px;padding:26px 0 44px;text-align:center}
footer code{font-family:var(--font-mono)}
[hidden]{display:none !important}

/* Plot width is a token so the axis header and every row stay locked. */
:root{--plot-w:104px}
@media(max-width:900px){:root{--plot-w:96px}}
@media(max-width:760px){
  :root{--plot-w:72px}
  .top .wrap{min-height:0;padding-top:10px;padding-bottom:10px}
  nav.tabs{margin-left:0;width:100%;order:3}
  .iv .range{display:none}
  .intro h1{font-size:21px}
  .wrap{padding:0 14px}
}
"""

_JS = r"""
(function () {
  "use strict";
  // Progressive enhancement only. Every table is already in the document;
  // this re-orders rows, hides them, and switches tabs. Nothing is built here.
  var root = document.documentElement;

  try {
    var saved = localStorage.getItem("obench-theme");
    if (saved) root.setAttribute("data-theme", saved);
  } catch (e) { /* storage disabled */ }

  document.getElementById("theme").addEventListener("click", function () {
    var now = root.getAttribute("data-theme")
      || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = now === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("obench-theme", next); } catch (e) { /* ignore */ }
  });

  // --- sorting ------------------------------------------------------------
  function renumber(tbody) {
    var n = 0;
    Array.prototype.forEach.call(tbody.rows, function (tr) {
      if (tr.hidden) return;
      n += 1;
      tr.cells[0].textContent = String(n);
    });
  }

  function sortBy(table, th) {
    var col = th.getAttribute("data-col");
    var numeric = th.getAttribute("data-type") !== "str";
    var was = th.getAttribute("aria-sort");
    var dir = was === "descending" ? "ascending" : "descending";
    var sign = dir === "ascending" ? 1 : -1;

    table.querySelectorAll("thead th").forEach(function (other) {
      other.removeAttribute("aria-sort");
      var arrow = other.querySelector(".arrow");
      if (arrow) arrow.textContent = "↕";
    });
    th.setAttribute("aria-sort", dir);
    var arrow = th.querySelector(".arrow");
    if (arrow) arrow.textContent = dir === "ascending" ? "↑" : "↓";

    var tbody = table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var x = a.getAttribute("data-s" + col);
      var y = b.getAttribute("data-s" + col);
      // Rows with no value for this measure always sink, either direction.
      if (x === "" && y === "") return 0;
      if (x === "") return 1;
      if (y === "") return -1;
      if (numeric) return (parseFloat(x) - parseFloat(y)) * sign;
      return x.localeCompare(y) * sign;
    });
    rows.forEach(function (tr) { tbody.appendChild(tr); });
    renumber(tbody);
  }

  document.querySelectorAll("table").forEach(function (table) {
    table.querySelectorAll("thead th.sortable").forEach(function (th) {
      th.addEventListener("click", function () { sortBy(table, th); });
      th.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); sortBy(table, th); }
      });
    });
  });

  // --- filtering ----------------------------------------------------------
  var controls = document.getElementById("controls");
  if (controls) {
    var q = document.getElementById("q");
    var fModel = document.getElementById("f-model");
    var fHarness = document.getElementById("f-harness");
    var fCaveats = document.getElementById("f-caveats");
    var noMatches = document.getElementById("no-matches");
    var boards = document.querySelectorAll("#view-harness .board[data-models]");

    function applyFilters() {
      var text = (q.value || "").trim().toLowerCase();
      var model = fModel ? fModel.value : "";
      var harness = fHarness ? fHarness.value : "";
      var hideCaveats = fCaveats && fCaveats.checked;
      var shown = 0;

      boards.forEach(function (board) {
        if (hideCaveats && board.getAttribute("data-caveats") === "1") {
          board.hidden = true;
          return;
        }
        var visible = 0;
        board.querySelectorAll("tbody tr").forEach(function (tr) {
          var hit = (!text || tr.getAttribute("data-search").indexOf(text) !== -1)
            && (!model || tr.getAttribute("data-model") === model)
            && (!harness || tr.getAttribute("data-harness") === harness);
          tr.hidden = !hit;
          if (hit) visible += 1;
        });
        board.hidden = visible === 0;
        if (visible) shown += 1;
        var tbody = board.querySelector("tbody");
        if (tbody) renumber(tbody);
      });
      if (noMatches) noMatches.hidden = shown !== 0;
    }

    [q, fModel, fHarness, fCaveats].forEach(function (el) {
      if (!el) return;
      el.addEventListener(el.tagName === "INPUT" && el.type === "search"
        ? "input" : "change", applyFilters);
    });
  }

  // --- tabs ---------------------------------------------------------------
  var VIEWS = ["harness", "router", "releases", "methodology"];

  function showView() {
    var hash = (location.hash || "").replace("#", "");
    var view = VIEWS.indexOf(hash) === -1 ? "harness" : hash;
    VIEWS.forEach(function (name) {
      document.getElementById("view-" + name).hidden = name !== view;
    });
    document.querySelectorAll("nav.tabs a").forEach(function (a) {
      if (a.getAttribute("href") === "#" + view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }
  window.addEventListener("hashchange", showView);
  showView();
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
        (str(harness["bundle_count"]), "harness bundles"),
        (str(len(harnesses)), "harnesses"),
        (str(len(models)), "models"),
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



# --------------------------------------------------------------------------
# Page — rendered here, enhanced in the browser
# --------------------------------------------------------------------------
#
# Every table is written as real HTML at build time. The script below only
# *enhances* what is already on the page: it re-orders rows, hides rows and
# boards, and switches tabs. Nothing is built client-side, so the page is
# complete with JavaScript switched off and there is exactly one renderer to
# keep honest.


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _attrs(mapping):
    out = []
    for key, value in mapping.items():
        if value is None or value is False:
            continue
        if value is True:
            out.append(f" {key}")
        else:
            out.append(f' {key}="{_esc(value)}"')
    return "".join(out)


def _tag(name, attrs=None, body=""):
    return f"<{name}{_attrs(attrs or {})}>{body}</{name}>"


def _fmt_pct(value, digits=1):
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_num(value):
    return "—" if value is None else f"{round(value):,}"


def _fmt_secs(value):
    return "—" if value is None else f"{value:.1f}s"


def _fmt_money(value, digits=3):
    return "—" if value is None else f"${value:.{digits}f}"


def _fmt_score(value):
    return "—" if value is None else f"{value:.3f}"


def _signed(value, fmt):
    if value is None:
        return "—"
    return ("+" if value > 0 else "") + fmt(value)


def _chip(text, cls="", title=None):
    return _tag("span", {"class": ("chip " + cls).strip(), "title": title}, _esc(text))


def _meta_field(label, value):
    return _tag("span", {}, _tag("b", {}, _esc(label) + " ") + _esc(value))


def _clamp01(value):
    return max(0.0, min(1.0, value))


def _interval_cell(estimate, low, high, fmt):
    """Value, a bar on the shared 0–100% track, and the numeric range."""
    marks = ""
    if low is not None and high is not None:
        lo, hi = _clamp01(low), _clamp01(high)
        marks += _tag("div", {
            "class": "span",
            "style": f"left:{lo * 100:.4f}%;width:{max(0.8, (hi - lo) * 100):.4f}%",
        })
    if estimate is not None:
        marks += _tag("div", {
            "class": "dot",
            "style": f"left:calc({_clamp01(estimate) * 100:.4f}% - 1px)",
        })
    range_text = "—" if low is None or high is None else f"{fmt(low)}–{fmt(high)}"
    return _tag("div", {"class": "iv"},
                _tag("span", {"class": "val"}, _esc(fmt(estimate)))
                + _tag("div", {"class": "track"}, marks)
                + _tag("span", {"class": "range"}, _esc(range_text)))


def _delta_cell(metric, fmt, higher_is_better, domain):
    """Signed contrast on a zero-centred axis shared by the whole column.

    Direction is carried by the sign, by the pole hue, and by which side of
    zero the bar sits on, so it never rests on colour alone. An interval
    covering zero is drawn neutral: no effect was detected.
    """
    estimate, low, high = metric["estimate"], metric["low"], metric["high"]
    known = low is not None and high is not None
    if known and low <= 0 <= high:
        tone = "null"
    elif estimate is not None and estimate != 0:
        better = estimate > 0 if higher_is_better else estimate < 0
        tone = "better" if better else "worse"
    else:
        tone = "null"

    def place(value):
        return max(0.0, min(100.0, (0.5 + (value / domain) * 0.5) * 100))

    marks = _tag("div", {"class": "zero"})
    if known:
        a, b = place(low), place(high)
        marks += _tag("div", {
            "class": "span " + tone,
            "style": f"left:{min(a, b):.4f}%;width:{max(0.8, abs(b - a)):.4f}%",
        })
    if estimate is not None:
        marks += _tag("div", {
            "class": "dot " + tone,
            "style": f"left:calc({place(estimate):.4f}% - 1px)",
        })
    title = ("95% CI " + _signed(low, fmt) + " to " + _signed(high, fmt)
             if known else "no interval available")
    return _tag("div", {"class": "dv", "title": title},
                _tag("span", {"class": "val " + tone}, _esc(_signed(estimate, fmt)))
                + _tag("div", {"class": "track"}, marks))


def _delta_domain(rows, key):
    """Widest bound in a contrast column, so its rows share one signed scale."""
    widest = 0.0
    for row in rows:
        metric = row.get(key) or {}
        for field in ("estimate", "low", "high"):
            value = metric.get(field)
            if value is not None:
                widest = max(widest, abs(value))
    return widest or 1.0


def _render_table(columns, rows, sort_index, row_attrs=None):
    """One table. ``columns`` entries are dicts:

    ``label``  header text
    ``cell``   row -> cell HTML
    ``key``    row -> sort key (omit for an unsortable column)
    ``type``   ``num`` or ``str`` (how the browser compares the key)
    ``dir``    default direction when the column is first clicked
    ``axis``   optional tick labels drawn under the header
    ``cls``    optional cell class
    ``skip_if_empty``  drop the column when no row has a key
    """
    columns = [
        col for col in columns
        if not col.get("skip_if_empty")
        or any(col["key"](row) is not None for row in rows)
    ]

    heads = ""
    for index, col in enumerate(columns):
        sortable = "key" in col
        attrs = {
            "scope": "col",
            "class": "sortable" if sortable else None,
            "data-type": col.get("type", "num") if sortable else None,
            "data-col": str(index) if sortable else None,
            "tabindex": "0" if sortable else None,
            "role": "button" if sortable else None,
        }
        if index == sort_index:
            attrs["aria-sort"] = "descending"
        body = _esc(col["label"])
        if sortable:
            arrow = "↓" if index == sort_index else "↕"
            body += _tag("span", {"class": "arrow"}, arrow)
        if col.get("axis"):
            body += _tag("div", {"class": "axis"},
                         "".join(_tag("span", {}, _esc(t)) for t in col["axis"]))
        heads += _tag("th", attrs, body)

    body_rows = ""
    for position, row in enumerate(rows, 1):
        cells = _tag("td", {"class": "rank"}, str(position))
        keys = {}
        for index, col in enumerate(columns):
            cells += _tag("td", {"class": col.get("cls")}, col["cell"](row))
            if "key" in col:
                value = col["key"](row)
                keys[f"data-s{index}"] = "" if value is None else str(value)
        attrs = dict(row_attrs(row) if row_attrs else {})
        attrs.update(keys)
        body_rows += _tag("tr", attrs, cells)

    return _tag("div", {"class": "scroll"}, _tag(
        "table", {},
        _tag("thead", {}, _tag("tr", {}, _tag("th", {"scope": "col"}, "#") + heads))
        + _tag("tbody", {}, body_rows)))


def _harness_board(bundle):
    arms = bundle["arms"]
    title = _esc(bundle["title"])
    if bundle.get("path"):
        title = _tag("a", {"href": bundle["path"]}, title)

    meta = "".join(filter(None, [
        _meta_field("kind", bundle["kind"]),
        _meta_field("date", bundle["date"]) if bundle.get("date") else None,
        _meta_field("denominators",
                    "matched (task, trial)" if bundle["table"] == "matched"
                    else "all countable"),
        _meta_field("cells", bundle["countable_rows"]),
        _meta_field("results", (bundle.get("results_sha256") or "")[:12])
        if bundle.get("results_sha256") else None,
        _meta_field("taskset", (bundle.get("task_set_digest") or "")[:12])
        if bundle.get("task_set_digest") else None,
    ]))

    chips = "".join(_chip(m) for m in bundle.get("models") or [])
    if bundle.get("has_caveats"):
        chips += _chip("caveats disclosed", "warn")

    caveats = ""
    if bundle.get("has_caveats"):
        items = "".join(_tag("li", {}, _esc(c)) for c in bundle["caveats"])
        caveats = _tag("details", {"class": "caveats"},
                       _tag("summary", {},
                            f"{len(bundle['caveats'])} caveat(s) from the release page")
                       + _tag("ul", {}, items))

    head = _tag("div", {"class": "head"},
                _tag("h2", {}, title)
                + _tag("div", {"class": "meta"}, meta)
                + (_tag("div", {"class": "chips"}, chips) if chips else "")
                + caveats)

    # The model is a header fact when a board pins one; only worth a column
    # when a board actually compares more than one.
    many_models = len({a["model"] for a in arms}) > 1

    columns = [
        {"label": "Harness", "cls": "name", "type": "str", "dir": "asc",
         "cell": lambda a: _esc(a["harness"]), "key": lambda a: a["harness"]},
    ]
    if many_models:
        columns.append(
            {"label": "Model", "type": "str", "dir": "asc",
             "cell": lambda a: _esc(a["model"]), "key": lambda a: a["model"]})
    columns += [
        {"label": "Solve rate · Wilson 95%", "axis": ["0", "50", "100%"],
         "cell": lambda a: _interval_cell(
             a["solve_rate"], (a.get("wilson95") or [None, None])[0],
             (a.get("wilson95") or [None, None])[1], _fmt_pct),
         "key": lambda a: a["solve_rate"]},
        {"label": "Solved",
         "cell": lambda a: f"{a['solved']}/{a['n']}",
         "key": lambda a: (a["solved"] / a["n"]) if a["n"] else None},
        {"label": "Median wall", "dir": "asc", "skip_if_empty": True,
         "cell": lambda a: _fmt_secs(a.get("median_wall_s")),
         "key": lambda a: a.get("median_wall_s")},
        {"label": "Tokens/solve", "dir": "asc", "skip_if_empty": True,
         "cell": lambda a: _fmt_num(a.get("total_tokens_per_solve")),
         "key": lambda a: a.get("total_tokens_per_solve")},
        {"label": "$/solve", "dir": "asc", "skip_if_empty": True,
         "cell": lambda a: _fmt_money(a.get("cost_per_solve_usd")),
         "key": lambda a: a.get("cost_per_solve_usd")},
        {"label": "Token basis",
         "cell": lambda a: _tag("div", {"class": "chips"}, "".join(
             _chip(b) for b in (a.get("token_bases") or ["unknown"])))},
    ]

    sort_index = next(i for i, c in enumerate(columns)
                      if c["label"].startswith("Solve rate"))
    table = _render_table(columns, arms, sort_index, row_attrs=lambda a: {
        "data-harness": a["harness"],
        "data-model": a["model"],
        "data-search": f"{a['harness']} {a['model']}".lower(),
    })

    links = "".join(filter(None, [
        _tag("span", {}, _tag("a", {"href": bundle["results_path"]}, "results.jsonl"))
        if bundle.get("results_path") else None,
        _tag("span", {}, _tag("a", {"href": bundle["path"]}, "release page"))
        if bundle.get("path") else None,
    ]))
    foot = _tag("div", {"class": "head"}, _tag("div", {"class": "meta"}, links))

    return _tag("section", {
        "class": "board",
        "data-models": ",".join(sorted({a["model"] for a in arms})),
        "data-harnesses": ",".join(sorted({a["harness"] for a in arms})),
        "data-caveats": "1" if bundle.get("has_caveats") else "0",
    }, head + table + foot)


def _router_board(bundle):
    title = _esc(bundle["title"])
    if bundle.get("path"):
        title = _tag("a", {"href": bundle["path"]}, title)

    meta = "".join(filter(None, [
        _meta_field("track", bundle.get("track") or "gateway_tax"),
        _meta_field("harness", bundle["harness"]) if bundle.get("harness") else None,
        _meta_field("date", bundle["date"]) if bundle.get("date") else None,
        _meta_field("blocks", f"{bundle['blocks_included']}/{bundle['blocks_observed']}"),
        _meta_field("tasks", bundle["tasks_included"]),
        _meta_field("lane", bundle["execution_lane"]) if bundle.get("execution_lane") else None,
        _meta_field("experiment", (bundle.get("experiment_digest") or "")[:12])
        if bundle.get("experiment_digest") else None,
    ]))
    excluded = bundle.get("blocks_excluded") or {}
    chips = "".join(_chip(f"excluded: {reason} × {count}", "warn")
                    for reason, count in sorted(excluded.items()))
    head = _tag("div", {"class": "head"},
                _tag("h2", {}, title)
                + _tag("div", {"class": "meta"}, meta)
                + (_tag("div", {"class": "chips"}, chips) if chips else ""))

    def cost_cell(arm):
        cost = arm.get("cost")
        if not cost:
            return "—"
        label = COST_BASIS_LABELS.get(cost["basis"], cost["basis"])
        body = _chip(label, title=cost["basis"])
        ratio = cost.get("coverage_ratio")
        if ratio is not None and ratio < 1:
            body += _chip(f"{_fmt_pct(ratio, 0)} covered", "warn")
        return _tag("div", {"class": "chips"}, body)

    columns = [
        {"label": "Route", "cls": "name", "type": "str", "dir": "asc",
         "cell": lambda a: _esc(a["arm_id"]), "key": lambda a: a["arm_id"]},
        {"label": "Role", "type": "str", "dir": "asc",
         "cell": lambda a: _tag("div", {"class": "chips"},
                                _chip(a.get("role") or "—",
                                      "role-direct" if a.get("role") == "direct" else "")
                                + (_chip(a["requested_provider"])
                                   if a.get("requested_provider") else "")),
         "key": lambda a: a.get("role") or ""},
        {"label": "Solve rate · 95% CI", "axis": ["0", "50", "100%"],
         "cell": lambda a: _interval_cell(
             a["solve_rate"]["estimate"], a["solve_rate"]["low"],
             a["solve_rate"]["high"], _fmt_pct),
         "key": lambda a: a["solve_rate"]["estimate"]},
        {"label": "Mean score",
         "cell": lambda a: _fmt_score(a["mean_checker_score"]["estimate"]),
         "key": lambda a: a["mean_checker_score"]["estimate"]},
        {"label": "Availability",
         "cell": lambda a: _fmt_pct(a["availability"]["estimate"]),
         "key": lambda a: a["availability"]["estimate"]},
        {"label": "Latency", "dir": "asc",
         "cell": lambda a: _fmt_secs(a["latency_s"]["estimate"]),
         "key": lambda a: a["latency_s"]["estimate"]},
        {"label": "$/solve", "dir": "asc", "skip_if_empty": True,
         "cell": lambda a: _fmt_money((a.get("cost") or {}).get("cost_per_solve_usd"), 4),
         "key": lambda a: (a.get("cost") or {}).get("cost_per_solve_usd")},
        {"label": "Cost basis", "skip_if_empty": True,
         "cell": cost_cell,
         "key": lambda a: a.get("cost") and a["cost"]["basis"]},
    ]
    parts = head + _render_table(columns, bundle["arms"], 2)

    contrasts = bundle.get("contrasts") or []
    if contrasts:
        def delta_column(label, key, fmt, higher_is_better, direction="desc"):
            domain = _delta_domain(contrasts, key)
            return {
                "label": label, "dir": direction,
                "cell": lambda r: _delta_cell(r[key], fmt, higher_is_better, domain),
                "key": lambda r: r[key]["estimate"],
            }

        tax_columns = [
            {"label": "Gateway arm", "cls": "name", "type": "str", "dir": "asc",
             "cell": lambda r: _esc(r["arm_id"]), "key": lambda r: r["arm_id"]},
            {"label": "vs direct", "cell": lambda r: _esc(r["direct_arm"])},
            delta_column("Δ solve rate", "solve_rate", _fmt_pct, True),
            delta_column("Δ mean score", "mean_checker_score", _fmt_score, True),
            delta_column("Δ availability", "availability", _fmt_pct, True),
            delta_column("Δ latency", "latency_s",
                         lambda v: f"{v:.2f}s", False, "asc"),
        ]
        legend = "".join(
            _tag("span", {}, _tag("i", {"class": tone}, "") + _esc(text))
            for tone, text in (
                ("better", "Gateway better than direct"),
                ("worse", "Gateway worse than direct"),
                ("null", "Interval spans zero — no detected effect"),
            ))
        parts += _tag("div", {"class": "head"},
                      _tag("h2", {}, "Gateway tax")
                      + _tag("p", {},
                             "Paired, task-weighted difference from the direct "
                             "control arm, with bootstrap 95% intervals. Each "
                             "column is plotted on its own shared scale about a "
                             "zero line.")
                      + _tag("div", {"class": "legend"}, legend))
        parts += _render_table(tax_columns, contrasts, 2)

    return _tag("section", {"class": "board", "data-caveats": "0"}, parts)


def _skipped_board(title, blurb, entries):
    items = "".join(
        _tag("li", {}, _tag("strong", {}, _esc(e["id"])) + " — " + _esc(e["reason"]))
        for e in entries)
    return _tag("section", {"class": "board", "data-caveats": "0"},
                _tag("div", {"class": "head"},
                     _tag("h2", {}, _esc(title)) + _tag("p", {}, _esc(blurb)))
                + _tag("div", {"class": "head"},
                       _tag("ul", {"class": "records"}, items)))


def _records_section(title, blurb, items, anchor=None):
    if not items:
        return ""
    return _tag("section", {"class": "board", "id": anchor},
                _tag("div", {"class": "head"},
                     _tag("h2", {}, _esc(title)) + _tag("p", {}, _esc(blurb)))
                + _tag("div", {"class": "head"},
                       _tag("ul", {"class": "records"}, "".join(items))))


def _record(name_html, meta_parts, sub=None, extra=""):
    body = name_html
    meta = "  ·  ".join(str(p) for p in meta_parts if p)
    if meta:
        body += _tag("div", {"class": "sub"}, _esc(meta))
    if sub:
        body += _tag("div", {"class": "sub"}, _esc(sub))
    return _tag("li", {}, body + extra)


def _linked_title(entry):
    name = _esc(entry.get("title") or entry.get("id") or "")
    return (_tag("a", {"href": entry["path"]}, name) if entry.get("path")
            else _tag("strong", {}, name))


def _releases_section(entries):
    return _records_section(
        "Releases", "First-party published comparison bundles.",
        [_record(_linked_title(e),
                 [e.get("date"), ", ".join(e.get("models") or [])])
         for e in entries])


def _community_section(entries):
    items = []
    for entry in entries:
        extra = ""
        if entry.get("link"):
            extra = _tag("div", {"class": "sub"},
                         _tag("a", {"href": entry["link"], "rel": "nofollow noopener"},
                              "source"))
        items.append(_record(
            _linked_title(entry),
            [entry.get("date"),
             "@" + entry["submitter"] if entry.get("submitter") else None],
            sub=entry.get("claim") or entry.get("description"),
            extra=extra))
    return _records_section(
        "Community",
        "Third-party bundles re-verified by CI. Digests prove tamper-evidence, "
        "not that runs were not cherry-picked.",
        items, anchor="community")


def _packs_section(entries):
    items = []
    for entry in entries:
        name = entry.get("id") or ""
        if entry.get("latest"):
            name = f"{name}@{entry['latest']}"
        head = _tag("strong", {}, _esc(name))
        if entry.get("kind"):
            head += " " + _chip(entry["kind"])
        items.append(_record(
            head,
            [entry.get("license"), entry.get("source"),
             (entry.get("content_sha256") or "")[:12] or None],
            sub=entry.get("description")))
    return _records_section(
        "Packs",
        "Versioned task and harness packs "
        "(obench pack install org/name@version).",
        items, anchor="packs")


def _controls(doc):
    models, harnesses = set(), set()
    for bundle in doc["harness"]["bundles"]:
        for arm in bundle["arms"]:
            models.add(arm["model"])
            harnesses.add(arm["harness"])

    def select(control_id, label, values):
        options = _tag("option", {"value": ""}, _esc(label))
        options += "".join(_tag("option", {"value": v}, _esc(v)) for v in sorted(values))
        return _tag("select", {"id": control_id, "aria-label": label}, options)

    body = _tag("input", {
        "type": "search", "id": "q", "placeholder": "Filter by harness or model…",
        "aria-label": "Filter by harness or model",
    })
    if models:
        body += select("f-model", "All models", models)
    if harnesses:
        body += select("f-harness", "All harnesses", harnesses)
    body += _tag("label", {},
                 _tag("input", {"type": "checkbox", "id": "f-caveats"})
                 + "Hide boards with disclosed caveats")
    return _tag("div", {"class": "controls", "id": "controls"}, body)


def _harness_view(doc):
    family = doc["harness"]
    body = _controls(doc)
    body += _tag("div", {"class": "note"},
                 _tag("strong", {}, "How to read this. ") + _esc(family["note"]))
    body += "".join(_harness_board(b) for b in family["bundles"])
    body += _tag("section", {"class": "board", "id": "no-matches", "hidden": True},
                 _tag("div", {"class": "empty"},
                      _tag("p", {}, "No boards match the current filters.")))
    if family.get("skipped"):
        body += _skipped_board(
            f"Not ranked ({len(family['skipped'])})",
            "Published pages without machine-verifiable results, listed with the "
            "reason rather than dropped.",
            family["skipped"])
    return body


def _router_view(doc):
    family = doc["router"]
    body = _tag("div", {"class": "note"},
                _tag("strong", {}, "How to read this. ") + _esc(family["note"]))
    if not family["bundles"]:
        body += _tag("section", {"class": "board"}, _tag(
            "div", {"class": "empty"},
            _tag("p", {}, "No verified Router Bench bundles are published yet.")
            + _tag("p", {},
                   "Produce one with <code>obench router run</code>, publish it "
                   "with <code>obench router publish &lt;results&gt; "
                   "&lt;experiment&gt; docs/router/&lt;id&gt;</code>, then "
                   "rebuild with <code>obench site build</code>.")))
    body += "".join(_router_board(b) for b in family["bundles"])
    if family.get("skipped"):
        body += _skipped_board(
            f"Not ranked ({len(family['skipped'])})",
            "Directories under the router root that did not verify.",
            family["skipped"])
    return body


def _releases_view(doc):
    return (_releases_section(doc["releases"])
            + _community_section(doc["community"])
            + _packs_section(doc["packs"]))


def render_board_html(doc):
    """The whole page: content rendered here, behaviour layered on top."""
    stat_html = "".join(
        _tag("div", {"class": "stat"},
             _tag("b", {}, _esc(value)) + _tag("span", {}, _esc(label)))
        for value, label in _headline_stats(doc)
    )
    tabs = "".join(
        _tag("a", {"href": "#" + slug}, _esc(label))
        for slug, label in (
            ("harness", "Harness Bench"),
            ("router", "Router Bench"),
            ("releases", "Releases"),
            ("methodology", "Methodology"),
        )
    )
    theme_button = _tag("button", {
        "class": "theme", "id": "theme", "type": "button",
        "aria-label": "Toggle colour theme", "title": "Toggle colour theme",
    }, '<svg viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">'
       '<path d="M8 0a8 8 0 1 0 0 16A8 8 0 0 0 8 0zm0 1.5V14.5a6.5 6.5 0 0 1 0-13z"/>'
       "</svg>")

    masthead = _tag("header", {"class": "top"}, _tag(
        "div", {"class": "wrap"},
        _tag("div", {"class": "brand"},
             _tag("span", {"class": "cmd"}, "obench")
             + _tag("span", {"class": "what"}, "leaderboards"))
        + _tag("nav", {"class": "tabs"}, tabs)
        + theme_button))

    intro = _tag("div", {"class": "intro"},
                 _tag("h1", {}, "Same task, different wrapper "
                                "&mdash; and different wire.")
                 + _tag("p", {}, _esc(doc["cross_family_note"])))

    # Every view ships expanded. The script collapses them into tabs; without
    # it the nav degrades to jump links over one continuous page.
    views = (
        _tag("main", {"id": "view-harness"}, _harness_view(doc))
        + _tag("main", {"id": "view-router"}, _router_view(doc))
        + _tag("main", {"id": "view-releases"}, _releases_view(doc))
        + _tag("main", {"id": "view-methodology"}, _METHODOLOGY)
    )

    footer = _tag("footer", {},
                  "Generated by " + _tag("code", {}, "obench site")
                  + " &middot; static, self-contained, no third-party assets "
                  "&middot; " + _tag("a", {"href": "board.json"}, "board.json"))

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>OpenBench leaderboards</title>"
        '<meta name="description" content="Harness and serving-route leaderboards '
        'for OpenBench, built from digest-verified result bundles.">'
        f"<style>{_CSS}</style></head><body>"
        + masthead
        + '<div class="wrap">'
        + intro
        + _tag("div", {"class": "stats"}, stat_html)
        + views
        + footer
        + "</div>"
        + f"<script>{_JS}</script>"
        + "</body></html>\n"
    )


def write_board(site_dir, community_dir=None, router_dirs=None):
    """Build and write ``index.html`` + ``board.json`` under ``site_dir``."""
    site_dir = os.path.abspath(site_dir)
    doc = build_board(site_dir, community_dir=community_dir, router_dirs=router_dirs)
    json_path = os.path.join(site_dir, "board.json")
    html_path = os.path.join(site_dir, "index.html")
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
    build = sub.add_parser("build", help="write index.html + board.json")
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
        print(f"index.html  {info['html_path']}")
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
