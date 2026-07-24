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
.hidden{display:none}

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
  var COST_BASIS_LABEL = {
    invoice_reconciled: "invoice",
    router_reported: "router",
    frozen_list_estimate: "list est."
  };
  function chip(text, cls) { return el("span", { class: "chip " + (cls || ""), text: text }); }
  function metaField(label, value) {
    return el("span", null, [el("b", { text: label + " " }), value]);
  }

  // Interval cell. Every row plots against the same 0–100% track, gridded at
  // 25% steps by CSS, so rows are comparable down the column rather than each
  // bar being its own private scale.
  function ciCell(estimate, low, high, fmt) {
    var wrap = el("div", { class: "iv" });
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

  // A measure every arm leaves blank is noise, not information: drop the whole
  // column rather than printing a column of em-dashes.
  function usefulColumns(columns, rows) {
    return columns.filter(function (col) {
      if (!col.omitIfEmpty) return true;
      return rows.some(function (r) {
        var v = col.present ? col.present(r) : col.sort(r);
        return v !== null && v !== undefined;
      });
    });
  }

  function renderTable(key, allColumns, rows, defaultSortLabel) {
    var columns = usefulColumns(allColumns, rows);
    // Sort by column identity, not index, so dropping a column cannot shift it.
    var defaultSort = null;
    columns.forEach(function (col, i) {
      if (col.label === defaultSortLabel) defaultSort = i;
    });
    var state = sortState[key] || (sortState[key] = { index: defaultSort, dir: "desc" });
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

    // Columns that draw a signed plot need the column's own domain first.
    columns.forEach(function (col) { if (col.prepare) col.scale = col.prepare(rows); });

    var thead = el("thead");
    var hrow = el("tr");
    columns.forEach(function (col, i) {
      var attrs = { class: col.sort ? "sortable" : "", scope: "col" };
      if (state.index === i) attrs["aria-sort"] = state.dir === "asc" ? "ascending" : "descending";
      var th = el("th", attrs, [col.label]);
      if (col.sort) {
        th.appendChild(el("span", {
          class: "arrow",
          text: state.index === i ? (state.dir === "asc" ? "↑" : "↓") : "↕"
        }));
        th.setAttribute("tabindex", "0");
        th.setAttribute("role", "button");
        var resort = function () {
          if (state.index === i) state.dir = state.dir === "asc" ? "desc" : "asc";
          else { state.index = i; state.dir = col.defaultDir || "desc"; }
          render();
        };
        th.addEventListener("click", resort);
        th.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); resort(); }
        });
      }
      // Axis ticks belong to the column, not to each cell.
      if (col.axis) {
        th.appendChild(el("div", { class: "axis" },
          col.axis.map(function (t) { return el("span", { text: t }); })));
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

    // Provenance reads as one instrument line, not a row of loose chips.
    head.appendChild(el("div", { class: "meta" }, [
      metaField("kind", bundle.kind),
      bundle.date ? metaField("date", bundle.date) : null,
      metaField("denominators",
        bundle.table === "matched" ? "matched (task, trial)" : "all countable"),
      metaField("cells", String(bundle.countable_rows)),
      bundle.results_sha256 ? metaField("results", bundle.results_sha256.slice(0, 12)) : null,
      bundle.task_set_digest ? metaField("taskset", bundle.task_set_digest.slice(0, 12)) : null
    ]));
    var chips = el("div", { class: "chips" });
    (bundle.models || []).forEach(function (m) { chips.appendChild(chip(m)); });
    if (bundle.has_caveats) chips.appendChild(chip("caveats disclosed", "warn"));
    if (chips.childNodes.length) head.appendChild(chips);

    if (bundle.has_caveats) {
      var det = el("details", { class: "caveats" }, [
        el("summary", { text: bundle.caveats.length + " caveat(s) from the release page" })
      ]);
      var ul = el("ul");
      bundle.caveats.forEach(function (c) { ul.appendChild(el("li", { text: c })); });
      det.appendChild(ul);
      head.appendChild(det);
    }

    // The model is a header fact when a board pins one; only worth a column
    // when a board actually compares more than one.
    var distinctModels = {};
    arms.forEach(function (a) { distinctModels[a.model] = 1; });
    var manyModels = Object.keys(distinctModels).length > 1;

    var columns = [
      { label: "#", cls: "rank", cell: function (r, i) { return String(i + 1); } },
      {
        label: "Harness", cls: "name",
        cell: function (r) { return r.harness; },
        sort: function (r) { return r.harness; }, defaultDir: "asc"
      },
      {
        label: "Solve rate · Wilson 95%",
        axis: ["0", "50", "100%"],
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
        label: "Median wall", omitIfEmpty: true,
        cell: function (r) { return secs(r.median_wall_s); },
        sort: function (r) { return r.median_wall_s; }, defaultDir: "asc"
      },
      {
        label: "Tokens/solve", omitIfEmpty: true,
        cell: function (r) { return num(r.total_tokens_per_solve); },
        sort: function (r) { return r.total_tokens_per_solve; }, defaultDir: "asc"
      },
      {
        label: "$/solve", omitIfEmpty: true,
        cell: function (r) { return money(r.cost_per_solve_usd); },
        sort: function (r) { return r.cost_per_solve_usd; }, defaultDir: "asc"
      },
      {
        label: "Token basis",
        cell: function (r) {
          var box = el("div", { class: "chips" });
          (r.token_bases || []).forEach(function (b) { box.appendChild(chip(b)); });
          if (!(r.token_bases || []).length) box.appendChild(chip("unknown"));
          return box;
        }
      }
    ];
    if (manyModels) {
      columns.splice(2, 0, {
        label: "Model",
        cell: function (r) { return r.model; },
        sort: function (r) { return r.model; }, defaultDir: "asc"
      });
    }

    var foot = el("div", { class: "head" }, [
      el("div", { class: "meta" }, [
        bundle.results_path
          ? el("span", null, [el("a", { href: bundle.results_path, text: "results.jsonl" })])
          : null,
        bundle.path ? el("span", null, [el("a", { href: bundle.path, text: "release page" })]) : null
      ])
    ]);

    return el("section", { class: "board" }, [
      head, renderTable("h:" + bundle.id, columns, arms, "Solve rate · Wilson 95%"), foot
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
      var ul = el("ul", { class: "records" });
      fam.skipped.forEach(function (s) {
        ul.appendChild(el("li", null, [
          el("strong", { text: s.id }), " — " + s.reason
        ]));
      });
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "head" }, [
          el("h2", { text: "Not ranked (" + fam.skipped.length + ")" }),
          el("p", { text: "Published pages without machine-verifiable results, "
            + "listed with the reason rather than dropped." })
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
    head.appendChild(el("div", { class: "meta" }, [
      metaField("track", bundle.track || "gateway_tax"),
      bundle.harness ? metaField("harness", bundle.harness) : null,
      bundle.date ? metaField("date", bundle.date) : null,
      metaField("blocks", bundle.blocks_included + "/" + bundle.blocks_observed),
      metaField("tasks", String(bundle.tasks_included)),
      bundle.execution_lane ? metaField("lane", bundle.execution_lane) : null,
      bundle.experiment_digest
        ? metaField("experiment", bundle.experiment_digest.slice(0, 12)) : null
    ]));
    var excluded = Object.keys(bundle.blocks_excluded || {});
    if (excluded.length) {
      var chips = el("div", { class: "chips" });
      excluded.forEach(function (reason) {
        chips.appendChild(chip(
          "excluded: " + reason + " × " + bundle.blocks_excluded[reason], "warn"));
      });
      head.appendChild(chips);
    }

    var armCols = [
      { label: "#", cls: "rank", cell: function (r, i) { return String(i + 1); } },
      {
        label: "Route", cls: "name",
        cell: function (r) { return r.arm_id; },
        sort: function (r) { return r.arm_id; }, defaultDir: "asc"
      },
      {
        label: "Role",
        cell: function (r) {
          return el("div", { class: "chips" }, [
            chip(r.role || "—", r.role === "direct" ? "role-direct" : ""),
            r.requested_provider ? chip(r.requested_provider) : null
          ]);
        },
        sort: function (r) { return r.role; }, defaultDir: "asc"
      },
      {
        label: "Solve rate · 95% CI",
        axis: ["0", "50", "100%"],
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
        label: "$/solve", omitIfEmpty: true,
        cell: function (r) { return r.cost ? money(r.cost.cost_per_solve_usd, 4) : "—"; },
        sort: function (r) { return r.cost ? r.cost.cost_per_solve_usd : null; },
        defaultDir: "asc"
      },
      {
        label: "Cost basis", omitIfEmpty: true,
        present: function (r) { return r.cost; },
        cell: function (r) {
          if (!r.cost) return "—";
          var tag = chip(COST_BASIS_LABEL[r.cost.basis] || r.cost.basis);
          tag.setAttribute("title", r.cost.basis);
          var box = el("div", { class: "chips" }, [tag]);
          if (r.cost.coverage_ratio !== null && r.cost.coverage_ratio !== undefined
              && r.cost.coverage_ratio < 1) {
            box.appendChild(chip(pct(r.cost.coverage_ratio, 0) + " covered", "warn"));
          }
          return box;
        }
      }
    ];

    var parts = [head, renderTable("r:" + bundle.id, armCols, bundle.arms, "Solve rate · 95% CI")];

    if ((bundle.contrasts || []).length) {
      var asPct = function (v) { return pct(v); };
      var asScore = function (v) { return v.toFixed(3); };
      var asSecs = function (v) { return v.toFixed(2) + "s"; };
      var taxCols = [
        {
          label: "Gateway arm", cls: "name",
          cell: function (r) { return r.arm_id; },
          sort: function (r) { return r.arm_id; }, defaultDir: "asc"
        },
        { label: "vs direct", cell: function (r) { return r.direct_arm; } },
        deltaColumn("Δ solve rate", "solve_rate", asPct, true),
        deltaColumn("Δ mean score", "mean_checker_score", asScore, true),
        deltaColumn("Δ availability", "availability", asPct, true),
        deltaColumn("Δ latency", "latency_s", asSecs, false, "asc")
      ];
      parts.push(el("div", { class: "head" }, [
        el("h2", { text: "Gateway tax" }),
        el("p", {
          text: "Paired, task-weighted difference from the direct control arm, "
            + "with bootstrap 95% intervals. Each column is plotted on its own "
            + "shared scale about a zero line."
        }),
        el("div", { class: "legend" }, [
          el("span", null, [el("i", { class: "better" }), "Gateway better than direct"]),
          el("span", null, [el("i", { class: "worse" }), "Gateway worse than direct"]),
          el("span", null, [el("i", { class: "null" }), "Interval spans zero — no detected effect"])
        ])
      ]));
      parts.push(renderTable("t:" + bundle.id, taxCols, bundle.contrasts, "Δ solve rate"));
    }

    return el("section", { class: "board" }, parts);
  }

  // Widest bound in a contrast column, so every row shares one signed scale.
  function deltaDomain(rows, key) {
    var max = 0;
    rows.forEach(function (r) {
      var m = r[key];
      if (!m) return;
      ["estimate", "low", "high"].forEach(function (f) {
        if (m[f] !== null && m[f] !== undefined) max = Math.max(max, Math.abs(m[f]));
      });
    });
    return max || 1;
  }

  // Signed contrast against a zero line. Direction is carried three ways —
  // the sign in the text, the pole hue, and which side of zero the bar sits on
  // — so it never depends on colour alone. An interval covering zero is drawn
  // neutral, because "spans zero" means no effect was detected.
  function deltaCell(metric, fmt, higherIsBetter, domain) {
    var v = metric.estimate;
    var lo = metric.low, hi = metric.high;
    var known = lo !== null && lo !== undefined && hi !== null && hi !== undefined;
    var tone = "null";
    if (known && lo <= 0 && hi >= 0) tone = "null";
    else if (v !== null && v !== undefined && v !== 0) {
      tone = (higherIsBetter ? v > 0 : v < 0) ? "better" : "worse";
    }

    var wrap = el("div", { class: "dv" }, [
      el("span", { class: "val " + tone, text: signed(v, fmt) })
    ]);
    var track = el("div", { class: "track" }, [el("div", { class: "zero" })]);
    // Map [-domain, +domain] onto the track, zero at the midpoint.
    var place = function (value) {
      return (0.5 + (value / domain) * 0.5) * 100;
    };
    if (known) {
      var a = Math.max(0, Math.min(100, place(lo)));
      var b = Math.max(0, Math.min(100, place(hi)));
      var span = el("div", { class: "span " + tone });
      span.style.left = Math.min(a, b) + "%";
      span.style.width = Math.max(1, Math.abs(b - a)) + "%";
      track.appendChild(span);
    }
    if (v !== null && v !== undefined) {
      var dot = el("div", { class: "dot " + tone });
      dot.style.left = "calc(" + Math.max(0, Math.min(100, place(v))) + "% - 1px)";
      track.appendChild(dot);
    }
    wrap.appendChild(track);
    // Four contrast columns with a printed interval each is over-labelling:
    // the bar carries the interval, the value is the direct label, and the
    // exact bounds are one hover away.
    wrap.setAttribute("title", known
      ? "95% CI " + signed(lo, fmt) + " to " + signed(hi, fmt)
      : "no interval available");
    return wrap;
  }

  function deltaColumn(label, key, fmt, higherIsBetter, defaultDir) {
    return {
      label: label,
      prepare: function (rows) { return deltaDomain(rows, key); },
      cell: function (r) { return deltaCell(r[key], fmt, higherIsBetter, this.scale); },
      sort: function (r) { return r[key].estimate; },
      defaultDir: defaultDir || "desc"
    };
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
      var ul = el("ul", { class: "records" });
      entries.forEach(function (e) {
        var line = el("li");
        line.appendChild(e.path
          ? el("a", { href: e.path, text: e.title || e.id })
          : el("strong", { text: e.title || e.id }));
        var meta = [e.date, (e.models || []).join(", "), e.submitter].filter(Boolean).join("  ·  ");
        if (meta) line.appendChild(el("div", { class: "sub", text: meta }));
        if (e.description) line.appendChild(el("div", { class: "sub", text: e.description }));
        ul.appendChild(line);
      });
      host.appendChild(el("section", { class: "board" }, [
        el("div", { class: "head" }, [
          el("h2", { text: title }),
          blurb ? el("p", { text: blurb }) : null
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
        '<div class="brand"><span class="cmd">obench</span>'
        '<span class="what">leaderboards</span></div>'
        f'<nav class="tabs">{tabs}</nav>'
        '<button class="theme" id="theme" type="button" '
        'aria-label="Toggle colour theme" title="Toggle colour theme">'
        '<svg viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">'
        '<path d="M8 0a8 8 0 1 0 0 16A8 8 0 0 0 8 0zm0 1.5V14.5a6.5 6.5 0 0 1 0-13z"/>'
        "</svg></button>"
        "</div></header>"
        '<div class="wrap">'
        '<div class="intro"><h1>Same task, different wrapper — and different wire.</h1>'
        f"<p>{html.escape(doc['cross_family_note'])}</p></div>"
        f'<div class="stats">{stat_html}</div>'
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
