#!/usr/bin/env python3
"""Unified static leaderboard site for both OpenBench benchmark families.

``obench site build`` scans the GitHub Pages root and emits two artifacts:

* ``board.json`` — one machine-readable document covering **Harness Bench**
  (verified ``results.jsonl`` publish bundles, aggregated by
  :mod:`obench.leaderboard`) and **Gateway Bench** (verified ``gateway_bench``
  evidence bundles, aggregated by :mod:`obench.gateway_report`).
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
The two families are never merged either — a Gateway Bench arm is a serving
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
    "Scores are not comparable across bundles: task sets, trial counts, and "
    "timeout caps differ."
)

GATEWAY_NOTE = (
    "Each board is one Gateway Tax experiment: a direct control arm against "
    "gateway arms on the same model revision. Intervals are task-bootstrap."
)

CROSS_FAMILY_NOTE = (
    "Each board is one verified bundle, shown with its interval and its "
    "provenance. The two families share no denominators."
)

# Preferred cost basis when an arm reports several. Invoice reconciliation is
# ground truth; the gateway's own number is next; a frozen list price is an
# estimate and is labelled as one in the UI.
COST_BASIS_PREFERENCE = (
    "invoice_reconciled",
    "gateway_reported",
    "frozen_list_estimate",
)

# Short column labels; the full basis name stays available on hover.
COST_BASIS_LABELS = {
    "invoice_reconciled": "invoice",
    "gateway_reported": "gateway",
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
# Gateway Bench
# --------------------------------------------------------------------------


def gateway_verification_error(bundle_dir):
    """Return why a directory is not a verified gateway bundle, else ``None``."""
    provenance_path = os.path.join(bundle_dir, "provenance.json")
    if not os.path.isfile(provenance_path):
        return "no provenance.json (not a gateway evidence bundle)"
    try:
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "invalid provenance.json"
    if not isinstance(provenance, dict):
        return "invalid provenance.json"
    if provenance.get("bundle_kind") != "gateway_bench":
        return "not a gateway_bench bundle"
    from . import gateway_publish
    try:
        gateway_publish.verify_bundle(bundle_dir)
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


def aggregate_gateway_bundle(bundle_dir, *, site_dir=None, manifest_entry=None):
    """Aggregate one verified gateway bundle, or ``None`` when unusable."""
    from . import gateway_report

    if gateway_verification_error(bundle_dir) is not None:
        return None
    results_path = os.path.join(bundle_dir, "results.jsonl")
    try:
        rows = _read_jsonl(results_path)
        report = gateway_report.aggregate(rows)
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
        "family": "gateway",
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


def build_gateway_family(site_dir, gateway_dirs=None):
    """Scan gateway bundle roots and aggregate every verified bundle."""
    site_dir = os.path.abspath(site_dir)
    roots = list(gateway_dirs or [])
    if not roots:
        default_root = os.path.join(site_dir, "gateway")
        roots = [default_root] if os.path.isdir(default_root) else []

    manifest = {
        e["id"]: e
        for e in leaderboard._load_manifest_list(
            os.path.join(site_dir, "gateway.json"))
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
            error = gateway_verification_error(bundle_dir)
            if error:
                skipped.append({"id": name, "kind": "gateway", "reason": error})
                continue
            aggregated = aggregate_gateway_bundle(
                bundle_dir, site_dir=site_dir, manifest_entry=manifest.get(name)
            )
            if aggregated is None:
                skipped.append({
                    "id": name,
                    "kind": "gateway",
                    "reason": "rows did not aggregate into a Gateway Tax report",
                })
                continue
            bundles.append(aggregated)

    bundles.sort(key=lambda b: (-leaderboard._date_key(b.get("date")), b.get("id") or ""))
    skipped.sort(key=lambda s: s.get("id") or "")
    return {
        "note": GATEWAY_NOTE,
        "bundle_count": len(bundles),
        "bundles": bundles,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------


def build_board(site_dir, community_dir=None, gateway_dirs=None):
    """Build the combined two-family board document."""
    site_dir = os.path.abspath(site_dir)
    harness = build_harness_family(site_dir, community_dir=community_dir)
    gateway = build_gateway_family(site_dir, gateway_dirs=gateway_dirs)
    releases = leaderboard._load_manifest_list(os.path.join(site_dir, "releases.json"))
    community = leaderboard._load_manifest_list(os.path.join(site_dir, "community.json"))
    packs = leaderboard._load_manifest_list(os.path.join(site_dir, "packs.json"))
    return {
        "generated_by": "obench site",
        "schema_version": SCHEMA_VERSION,
        "cross_family_note": CROSS_FAMILY_NOTE,
        "harness": harness,
        "gateway": gateway,
        "releases": [e for e in releases if isinstance(e, dict)],
        "community": [e for e in community if isinstance(e, dict)],
        "packs": [e for e in packs if isinstance(e, dict)],
    }


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

_CSS = """
/* ---------------------------------------------------------------------------
   OpenBench leaderboards.

   Treated as a published measurement rather than a dashboard.

   Type      three roles. A serif display face carries headlines and board
             titles; a sans carries prose and small labels; a mono carries
             every identifier the benchmark names and every value it measures,
             because those are readings rather than writing.
   Colour    the page is ink on paper. Colour is reserved for data: the
             interval marks, and the validated blue/orange diverging pair on
             signed contrasts. Chrome never spends it. Marks carry the
             validated hue; any text tinted by a pole uses the darker
             `*-ink` step, so no reading depends on a sub-4.5:1 colour.
   Structure rules, not boxes. Boards are records separated by hairlines, so
             the data sits on the page instead of inside a card.
--------------------------------------------------------------------------- */
:root{
  color-scheme:light;
  --font-display:"Iowan Old Style","Palatino Linotype",Palatino,Charter,
    "Bitstream Charter",Georgia,"Liberation Serif",serif;
  --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
    "Helvetica Neue",Arial,sans-serif;
  --font-mono:ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono",
    "Cascadia Mono",Menlo,Consolas,"Liberation Mono",monospace;

  --paper:#ffffff;
  --ground:#f2f2f0;
  --ink:#0f1113;
  --ink-2:#54595f;
  --ink-3:#6b7280;
  --rule:#dfe0dd;
  --rule-strong:#b9bcb8;
  --wash:#f7f7f5;

  --data:#2a78d6;
  --data-rgb:42,120,214;
  --pole-better:#2a78d6;
  --pole-better-rgb:42,120,214;
  --pole-worse:#eb6834;
  --pole-worse-rgb:235,104,52;
  --pole-better-ink:#1f66bd;
  --pole-worse-ink:#ad4218;
  --pole-null:#6b7280;
  --pole-null-rgb:107,114,128;

  --warn-ink:#8a5300;
  --warn-bg:#fbf1dc;
  --warn-rule:#e0c58a;

  --track:rgba(15,17,19,.08);
  --grid:rgba(15,17,19,.14);
  --plot-w:140px;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --paper:#101215;
    --ground:#08090b;
    --ink:#eceef1;
    --ink-2:#a2a9b2;
    --ink-3:#8b939d;
    --rule:#22262b;
    --rule-strong:#39404a;
    --wash:#161a1f;

    --data:#3987e5;
    --data-rgb:57,135,229;
    --pole-better:#3987e5;
    --pole-better-rgb:57,135,229;
    --pole-worse:#d95926;
    --pole-worse-rgb:217,89,38;
    --pole-better-ink:#3987e5;
    --pole-worse-ink:#e06a3c;
    --pole-null:#8b939d;
    --pole-null-rgb:139,147,157;

    --warn-ink:#f0b545;
    --warn-bg:#241d10;
    --warn-rule:#4b3c17;

    --track:rgba(236,238,241,.10);
    --grid:rgba(236,238,241,.16);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --paper:#101215;
  --ground:#08090b;
  --ink:#eceef1;
  --ink-2:#a2a9b2;
  --ink-3:#8b939d;
  --rule:#22262b;
  --rule-strong:#39404a;
  --wash:#161a1f;

  --data:#3987e5;
  --data-rgb:57,135,229;
  --pole-better:#3987e5;
  --pole-better-rgb:57,135,229;
  --pole-worse:#d95926;
  --pole-worse-rgb:217,89,38;
  --pole-better-ink:#3987e5;
  --pole-worse-ink:#e06a3c;
  --pole-null:#8b939d;
  --pole-null-rgb:139,147,157;

  --warn-ink:#f0b545;
  --warn-bg:#241d10;
  --warn-rule:#4b3c17;

  --track:rgba(236,238,241,.10);
  --grid:rgba(236,238,241,.16);
}
:root[data-theme="light"]{
  color-scheme:light;
  --paper:#ffffff;
  --ground:#f2f2f0;
  --ink:#0f1113;
  --ink-2:#54595f;
  --ink-3:#6b7280;
  --rule:#dfe0dd;
  --rule-strong:#b9bcb8;
  --wash:#f7f7f5;

  --data:#2a78d6;
  --data-rgb:42,120,214;
  --pole-better:#2a78d6;
  --pole-better-rgb:42,120,214;
  --pole-worse:#eb6834;
  --pole-worse-rgb:235,104,52;
  --pole-better-ink:#1f66bd;
  --pole-worse-ink:#ad4218;
  --pole-null:#6b7280;
  --pole-null-rgb:107,114,128;

  --warn-ink:#8a5300;
  --warn-bg:#fbf1dc;
  --warn-rule:#e0c58a;

  --track:rgba(15,17,19,.08);
  --grid:rgba(15,17,19,.14);
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font:16px/1.6 var(--font-sans);-webkit-font-smoothing:antialiased;
  font-variant-numeric:tabular-nums}
[hidden]{display:none !important}
a{color:inherit;text-decoration-color:var(--rule-strong);text-underline-offset:3px}
a:hover{text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--data);outline-offset:3px;border-radius:2px}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms !important;transition-duration:.01ms !important}
}

.wrap{max-width:1180px;margin:0 auto;padding:0 32px}

/* --- masthead ----------------------------------------------------------- */
header.top{border-bottom:1px solid var(--ink);background:var(--paper);
  position:sticky;top:0;z-index:30}
.top .wrap{display:flex;align-items:center;gap:26px;min-height:56px;flex-wrap:wrap}
.brand{display:flex;align-items:baseline;gap:9px;white-space:nowrap;
  margin-right:auto}
.brand .cmd{font:600 13px/1 var(--font-mono);letter-spacing:.16em;
  text-transform:uppercase}
.brand .what{font:italic 15px/1 var(--font-display);color:var(--ink-2)}
nav.tabs{display:flex;gap:24px;flex-wrap:wrap}
nav.tabs a{font:600 13px/1 var(--font-sans);letter-spacing:.03em;
  text-decoration:none;color:var(--ink-3);padding:19px 0;
  border-bottom:2px solid transparent;margin-bottom:-1px}
nav.tabs a:hover{color:var(--ink)}
nav.tabs a[aria-current="page"]{color:var(--ink);border-bottom-color:var(--ink)}
button.theme{background:none;border:0;color:var(--ink-3);cursor:pointer;
  padding:6px;display:inline-flex;align-items:center}
button.theme:hover{color:var(--ink)}
button.theme svg{width:15px;height:15px}

/* --- the lede ----------------------------------------------------------- */
.lede{padding:56px 0 36px;border-bottom:1px solid var(--rule)}
.lede h1{margin:0;max-width:26ch;font:400 clamp(30px,4vw,46px)/1.08
  var(--font-display);letter-spacing:-.02em;text-wrap:balance}
.lede .deck{margin:22px 0 0;max-width:56ch;font-size:18.5px;line-height:1.55;
  color:var(--ink-2)}
.lede .dateline{margin-top:30px;padding-top:14px;border-top:1px solid var(--rule);
  font:11.5px/1.7 var(--font-mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--ink-3);display:flex;flex-wrap:wrap;gap:0 22px}

/* --- controls ----------------------------------------------------------- */
.controls{display:flex;gap:14px;flex-wrap:wrap;align-items:center;
  padding:20px 0;border-bottom:1px solid var(--rule)}
.controls input[type=search],.controls select{background:transparent;
  color:var(--ink);border:0;border-bottom:1px solid var(--rule-strong);
  padding:6px 2px;font:13px var(--font-mono);border-radius:0}
.controls input[type=search]{min-width:230px;flex:1}
.controls select{font-family:var(--font-sans);font-size:13.5px;cursor:pointer}
.controls label{display:flex;align-items:center;gap:8px;color:var(--ink-2);
  font-size:13.5px}

/* --- asides ------------------------------------------------------------- */
.note{margin:22px 0 0;padding:0;color:var(--ink-2);font-size:15px;max-width:78ch}
.note strong{color:var(--ink);font-weight:600}

/* --- boards, as records rather than cards ------------------------------- */
.board{padding:52px 0 8px;border-bottom:1px solid var(--rule)}
.board:last-child{border-bottom:0}
.board .head{padding:0 0 18px}
.board .head:last-child{padding:16px 0 0;border-top:1px solid var(--rule)}
.scroll+.head{padding-top:40px}
.board h2{margin:0;font:400 27px/1.2 var(--font-display);letter-spacing:-.012em;
  max-width:70ch;text-wrap:balance;hyphens:none}
.board h2 a{text-decoration:none}
.board h2 a:hover{text-decoration:underline}
.board .head p{margin:10px 0 0;color:var(--ink-2);font-size:14.5px;max-width:74ch}
.meta{margin-top:11px;font:11.5px/1.9 var(--font-mono);letter-spacing:.05em;
  color:var(--ink-3);display:flex;flex-wrap:wrap;gap:0 20px;
  text-transform:uppercase}
.meta b{font-weight:400;color:var(--ink-3);opacity:.65}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}
.chip{border:1px solid var(--rule-strong);color:var(--ink-2);
  padding:2px 8px;font:500 11px/1.6 var(--font-mono);white-space:nowrap;
  letter-spacing:.02em}
.chip.warn{background:var(--warn-bg);border-color:var(--warn-rule);
  color:var(--warn-ink)}
.chip.role-direct{border-color:var(--ink);color:var(--ink)}
td .chips{flex-wrap:nowrap;justify-content:flex-end;margin-top:0}

details.caveats{margin-top:14px;font-size:14.5px}
details.caveats summary{cursor:pointer;color:var(--warn-ink);font-weight:600;
  list-style:none;display:flex;align-items:center;gap:7px;font-size:13.5px}
details.caveats summary::-webkit-details-marker{display:none}
details.caveats summary::before{content:"+";font:600 15px var(--font-mono)}
details.caveats[open] summary::before{content:"\\2212"}
details.caveats ul{margin:12px 0 0;padding-left:19px;color:var(--ink-2)}
details.caveats li{margin:8px 0;max-width:80ch}

/* --- tables ------------------------------------------------------------- */
.scroll{overflow-x:auto;margin:0 -4px;padding:0 4px}
table{border-collapse:collapse;width:100%}
th,td{padding:13px 11px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:0}
th:last-child,td:last-child{padding-right:0}
thead th{color:var(--ink-3);font:600 10.5px/1.4 var(--font-sans);
  text-transform:uppercase;letter-spacing:.1em;vertical-align:bottom;
  padding-bottom:11px;border-bottom:1px solid var(--ink)}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:var(--ink)}
thead th .arrow{opacity:.35;margin-left:5px;font-size:9px;vertical-align:1px}
thead th[aria-sort]{color:var(--ink)}
thead th[aria-sort] .arrow{opacity:1}
tbody td{font:14px/1.4 var(--font-mono);color:var(--ink-2);
  border-bottom:1px solid var(--rule)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--wash)}
td.rank{width:1%;padding-right:6px;font:400 19px/1 var(--font-display);
  color:var(--ink-3)}
td.name{color:var(--ink);font-weight:600;letter-spacing:-.01em}
/* The leading visible row of the current sort carries the emphasis. */
tbody tr.lead td{border-bottom-color:var(--rule-strong)}
tbody tr.lead td.rank{color:var(--ink)}
tbody tr.lead td.name{font-size:15px}
tbody tr.lead .iv .val{font-size:16px;color:var(--ink)}
/* Identity stays put while the measures scroll. */
thead th:first-child,tbody td:first-child{position:sticky;left:0;z-index:1;
  background:var(--paper)}
tbody tr:hover td:first-child{background:var(--wash)}

/* --- interval plot: one shared 0-100% scale for the whole column -------- */
.scroll[data-dense="1"] .iv .range{display:none}
.scroll[data-dense="1"] th,.scroll[data-dense="1"] td{padding-left:9px;
  padding-right:9px}
.axis{display:flex;justify-content:space-between;width:var(--plot-w);
  margin:7px 0 0 auto;font:400 9.5px/1 var(--font-mono);color:var(--ink-3);
  letter-spacing:.02em;text-transform:none}
.iv{display:flex;align-items:center;gap:11px;justify-content:flex-end}
.iv .val{flex:0 0 auto;min-width:56px;text-align:right;color:var(--ink);
  font-weight:600;font-size:14.5px}
.iv .track{flex:0 0 var(--plot-w);position:relative;width:var(--plot-w);
  height:5px;background:var(--track);border-radius:1px;
  background-image:linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:25% 100%}
.iv .span{position:absolute;top:0;height:5px;border-radius:1px;
  background:rgba(var(--data-rgb),.40)}
.iv .dot{position:absolute;top:-3px;width:3px;height:11px;border-radius:1px;
  background:var(--data)}
.iv .range{flex:0 0 auto;min-width:84px;text-align:right;color:var(--ink-3);
  font-size:11.5px}

/* --- signed contrast: diverging about a zero line ----------------------- */
.dv{display:flex;align-items:center;gap:11px;justify-content:flex-end}
.dv .val{flex:0 0 auto;min-width:62px;text-align:right;font-weight:600;
  font-size:14.5px}
.dv .val.better{color:var(--pole-better-ink)}
.dv .val.worse{color:var(--pole-worse-ink)}
.dv .val.null{color:var(--ink-2)}
.dv .track{flex:0 0 var(--plot-w);position:relative;width:var(--plot-w);
  height:5px;background:var(--track);border-radius:1px}
.dv .zero{position:absolute;left:50%;top:-4px;width:1px;height:13px;
  background:var(--rule-strong)}
.dv .span{position:absolute;top:0;height:5px;border-radius:1px}
.dv .span.better{background:rgba(var(--pole-better-rgb),.40)}
.dv .span.worse{background:rgba(var(--pole-worse-rgb),.40)}
.dv .span.null{background:rgba(var(--pole-null-rgb),.34)}
.dv .dot{position:absolute;top:-3px;width:3px;height:11px;border-radius:1px}
.dv .dot.better{background:var(--pole-better)}
.dv .dot.worse{background:var(--pole-worse)}
.dv .dot.null{background:var(--pole-null)}
.legend{display:flex;gap:22px;flex-wrap:wrap;margin-top:14px;
  font-size:13px;color:var(--ink-2)}
.legend span{display:flex;align-items:center;gap:8px}
.legend i{width:16px;height:5px;border-radius:1px;display:inline-block}
.legend i.better{background:var(--pole-better)}
.legend i.worse{background:var(--pole-worse)}
.legend i.null{background:var(--pole-null)}

/* --- lists and prose ---------------------------------------------------- */
.empty{padding:4px 0 32px;color:var(--ink-2);max-width:62ch}
.empty p{margin:0 0 10px}
code{font-family:var(--font-mono);font-size:.86em}
ul.records{list-style:none;margin:0;padding:0}
ul.records li{padding:16px 0;border-bottom:1px solid var(--rule)}
ul.records li:last-child{border-bottom:0}
ul.records a,ul.records strong{font:500 17px/1.35 var(--font-display);
  text-decoration:none}
ul.records a:hover{text-decoration:underline}
ul.records .sub{color:var(--ink-3);font:11.5px/1.8 var(--font-mono);
  letter-spacing:.04em;margin-top:3px}
.prose{padding:52px 0;max-width:70ch}
.prose h2{margin:0 0 10px;font:400 27px/1.2 var(--font-display);
  letter-spacing:-.012em}
.prose h3{margin:34px 0 8px;font:600 11px/1.4 var(--font-sans);
  text-transform:uppercase;letter-spacing:.1em;color:var(--ink-3)}
.prose p,.prose li{color:var(--ink-2);font-size:15.5px}
.prose li{margin:9px 0}
.prose strong{color:var(--ink)}
footer{color:var(--ink-3);font-size:12.5px;padding:28px 0 60px;
  border-top:1px solid var(--rule)}

@media(max-width:1080px){:root{--plot-w:112px}}
@media(max-width:860px){
  :root{--plot-w:96px}
  .iv .range{display:none}
  .wrap{padding:0 20px}
  .lede{padding:44px 0 30px}
  nav.tabs{gap:18px}
}
@media(max-width:680px){
  :root{--plot-w:70px}
  .top .wrap{min-height:0;padding-top:10px;padding-bottom:0;gap:12px}
  nav.tabs{order:3;width:100%;gap:20px;overflow-x:auto}
  nav.tabs a{padding:10px 0}
  .brand{margin-right:0}
  .lede h1{font-size:31px}
  .lede .deck{font-size:16.5px}
  .board{padding:34px 0 8px}
  .board h2{font-size:22px}
  th,td{padding:11px 10px}
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
      tr.classList.remove("lead");
      if (tr.hidden) return;
      n += 1;
      tr.cells[0].textContent = String(n);
      // Emphasis belongs to the leading *visible* row, not to whichever row
      // happens to sit first in the DOM after a filter.
      if (n === 1) tr.classList.add("lead");
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
  var VIEWS = ["harness", "gateway", "releases", "methodology"];

  function showView() {
    var hash = (location.hash || "").replace("#", "");
    var view = "harness";
    if (VIEWS.indexOf(hash) !== -1) {
      view = hash;
    } else if (hash) {
      // A deep link to a section (#community, #packs) should open the view
      // that section lives in rather than silently falling back.
      var target = document.getElementById(hash);
      var host = target && target.closest('main[id^="view-"]');
      if (host) view = host.id.replace("view-", "");
    }
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

def _lede(doc):
    """Page title, one line on what is shown, and a count of what is covered.

    Deliberately draws no conclusion from the data: the boards carry the
    results, and readers do their own reading.
    """
    bundles = doc["harness"]["bundles"]
    harness = doc["harness"]
    gateway = doc["gateway"]
    harnesses = {a["harness"] for b in bundles for a in b["arms"]}
    models = {a["model"] for b in bundles for a in b["arms"]}
    cells = sum(b.get("countable_rows") or 0 for b in bundles)
    dates = sorted(b["date"] for b in bundles if b.get("date"))

    facts = [
        f"{len(harnesses)} harnesses",
        f"{len(models)} models",
        f"{harness['bundle_count']} verified bundles",
        f"{cells:,} countable cells",
    ]
    if gateway["bundle_count"]:
        facts.append(f"{gateway['bundle_count']} gateway bundles")
    if dates:
        facts.append(f"updated {dates[-1]}")
    return (
        "Harness and serving-route leaderboards",
        doc["cross_family_note"],
        facts,
    )


_METHODOLOGY = """
<section class="prose">
  <h2>What is being measured</h2>
  <p>OpenBench runs two benchmark families. They share a task contract and a
  checker, and no denominators.</p>

  <h3>Harness Bench</h3>
  <p>Varies the coding-agent harness — the CLI that wraps a model in a run loop,
  tool set, and permission policy — while holding the model and task fixed. An
  arm is <code>(harness, model)</code>. A task is solved when its
  <code>checker.sh</code> exits 0; the harness's own claim of success is never
  trusted.</p>

  <h3>Gateway Bench</h3>
  <p>Holds the harness, model, provider, sampling, task, and budget fixed while
  varying the serving route. An arm is a route. The implemented track is
  <strong>Gateway Tax</strong>: a direct baseline against one or more gateway
  arms on the same canonical model revision, with fallbacks, gateway retries,
  and caching disabled.</p>

  <h2>Denominators and intervals</h2>
  <ul>
    <li>Denominators are countable cells. Infrastructure and rate-limit failures
    are excluded; other failures, including timeouts, stay in the denominator.</li>
    <li>Harness Bench uses Wilson 95% intervals over matched
    <code>(task, trial)</code> cells whenever a bundle has two or more arms.</li>
    <li>Gateway Bench uses task-weighted estimates with bootstrap 95% intervals,
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
    price. Gateway cost prefers invoice reconciliation, then the gateway's own
    reported cost, then a frozen list-price estimate, and shows coverage when a
    basis does not cover every call.</li>
    <li>Harness defaults are not clamped.</li>
  </ul>

  <h2>Comparability</h2>
  <ul>
    <li>Cells from different bundles are never blended. Each board is one
    bundle; cross-bundle ranking on different task sets is not supported.</li>
    <li>Every ranked bundle ships <code>results.jsonl</code> plus a provenance
    digest and is re-verified before it appears here. Digests show
    tamper-evidence, not absence of cherry-picking.</li>
    <li>Results cover only the included tasks, trials, model deployments,
    harness versions, and timeout caps.</li>
  </ul>

  <h2>Reproducing a board</h2>
  <p>Every board links its <code>results.jsonl</code>. Re-check a bundle with
  <code>obench verify &lt;bundle&gt;</code> (harness) or
  <code>obench gateway verify &lt;bundle&gt;</code> (gateway), and rebuild this
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


def _sort_key(value, descending):
    """Order strings alphabetically and numbers by magnitude, either way."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.lower() if not descending else _ReversedText(value.lower())
    return -value if descending else value


class _ReversedText(str):
    """A string that compares backwards, so text can sort descending."""

    def __lt__(self, other):
        return str.__gt__(self, other)

    def __gt__(self, other):
        return str.__lt__(self, other)


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


def _render_table(columns, rows, sorted_by=None, row_attrs=None):
    """One table. ``columns`` entries are dicts:

    ``label``  header text
    ``cell``   row -> cell HTML
    ``key``    row -> sort key (omit for an unsortable column)
    ``type``   ``num`` or ``str`` (how the browser compares the key)
    ``dir``    default direction when the column is first clicked
    ``axis``   optional tick labels drawn under the header
    ``cls``    optional cell class
    ``plot``   the cell draws a plot (used to budget the plot width)
    ``skip_if_empty``  drop the column when no row has a key

    ``sorted_by`` is a column label. When given, the rows are *actually* sorted
    by that column before rendering, so the header's sort indicator and the
    rank numbers agree with what is on screen. When omitted, the caller's own
    ordering stands and no column claims to be sorted — which is what a table
    with a control arm wants, since its first row is a baseline and not a rank.
    """
    columns = [
        col for col in columns
        if not col.get("skip_if_empty")
        or any(col["key"](row) is not None for row in rows)
    ]

    sort_index = None
    if sorted_by is not None:
        sort_index = next(
            (i for i, col in enumerate(columns) if col["label"] == sorted_by), None)
    if sort_index is not None:
        col = columns[sort_index]
        descending = col.get("dir", "desc") == "desc"
        # Stable, so the caller's ordering survives as the tie-break, and rows
        # with nothing to compare sink either way.
        rows = sorted(
            rows,
            key=lambda r: (col["key"](r) is None,
                           _sort_key(col["key"](r), descending)),
        )

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
            attrs["aria-sort"] = (
                "descending" if col.get("dir", "desc") == "desc" else "ascending")
        body = _esc(col["label"])
        if sortable:
            arrow = "↕"
            if index == sort_index:
                arrow = "↓" if col.get("dir", "desc") == "desc" else "↑"
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
        if position == 1:
            attrs["class"] = "lead"
        body_rows += _tag("tr", attrs, cells)

    # Plot width is a per-table budget: four contrast columns cannot each be as
    # wide as a single solve-rate column. Dense tables also drop the printed
    # interval, which the bar already carries.
    plots = sum(1 for col in columns if col.get("plot"))
    plot_w = {0: 140, 1: 148, 2: 116, 3: 100}.get(plots, 84)
    if len(columns) > 7:
        plot_w = min(plot_w, 116)
    attrs = {"class": "scroll", "style": f"--plot-w:{plot_w}px"}
    if len(columns) > 6 or plots > 2:
        attrs["data-dense"] = "1"
    return _tag("div", attrs, _tag(
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
        {"label": "Solve rate · Wilson 95%", "axis": ["0", "50", "100%"], "plot": True,
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

    table = _render_table(columns, arms, "Solve rate · Wilson 95%", row_attrs=lambda a: {
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


def _gateway_board(bundle):
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
        {"label": "Solve rate · 95% CI", "axis": ["0", "50", "100%"], "plot": True,
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
    parts = head + _render_table(columns, bundle["arms"])

    contrasts = bundle.get("contrasts") or []
    if contrasts:
        def delta_column(label, key, fmt, higher_is_better, direction="desc"):
            domain = _delta_domain(contrasts, key)
            return {
                "label": label, "dir": direction, "plot": True,
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
                             "Paired difference from the direct control arm, "
                             "bootstrap 95% intervals.")
                      + _tag("div", {"class": "legend"}, legend))
        parts += _render_table(tax_columns, contrasts)

    return _tag("section", {"class": "board", "data-caveats": "0"}, parts)


def _skipped_board(title, blurb, entries):
    items = "".join(
        _tag("li", {},
             _tag("strong", {}, _esc(e["id"]))
             + _tag("div", {"class": "sub"}, _esc(e["reason"])))
        for e in entries)
    return _tag("section", {"class": "board", "data-caveats": "0"},
                _tag("div", {"class": "head"},
                     _tag("h2", {}, _esc(title)) + _tag("p", {}, _esc(blurb)))
                + _tag("ul", {"class": "records"}, items))


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
        "Releases", "First-party bundles.",
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
        "Third-party bundles, re-verified by CI. Digests show tamper-evidence, "
        "not absence of cherry-picking.",
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
        "Versioned task and harness packs.",
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
    body += _tag("p", {"class": "note"}, _esc(family["note"]))
    body += "".join(_harness_board(b) for b in family["bundles"])
    body += _tag("section", {"class": "board", "id": "no-matches", "hidden": True},
                 _tag("div", {"class": "empty"},
                      _tag("p", {}, "No boards match the current filters.")))
    if family.get("skipped"):
        body += _skipped_board(
            f"Not ranked ({len(family['skipped'])})",
            "No machine-verifiable results.",
            family["skipped"])
    return body


def _gateway_view(doc):
    family = doc["gateway"]
    body = _tag("p", {"class": "note"}, _esc(family["note"]))
    if not family["bundles"]:
        body += _tag("section", {"class": "board"}, _tag(
            "div", {"class": "empty"},
            _tag("p", {}, "No verified Gateway Bench bundles published yet.")))
    body += "".join(_gateway_board(b) for b in family["bundles"])
    if family.get("skipped"):
        body += _skipped_board(
            f"Not ranked ({len(family['skipped'])})",
            "Did not verify.",
            family["skipped"])
    return body


def _releases_view(doc):
    return (_releases_section(doc["releases"])
            + _community_section(doc["community"])
            + _packs_section(doc["packs"]))


def render_board_html(doc):
    """The whole page: content rendered here, behaviour layered on top."""
    title, deck, facts = _lede(doc)
    lede = _tag("div", {"class": "lede"},
                _tag("h1", {}, _esc(title))
                + _tag("p", {"class": "deck"}, _esc(deck))
                + _tag("div", {"class": "dateline"},
                       "".join(_tag("span", {}, _esc(f)) for f in facts)))
    tabs = "".join(
        _tag("a", {"href": "#" + slug}, _esc(label))
        for slug, label in (
            ("harness", "Harness Bench"),
            ("gateway", "Gateway Bench"),
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
             _tag("span", {"class": "name"}, "OpenBench")
             + _tag("span", {"class": "what"}, "leaderboards"))
        + _tag("nav", {"class": "tabs"}, tabs)
        + theme_button))

    # Every view ships expanded. The script collapses them into tabs; without
    # it the nav degrades to jump links over one continuous page.
    views = (
        _tag("main", {"id": "view-harness"}, _harness_view(doc))
        + _tag("main", {"id": "view-gateway"}, _gateway_view(doc))
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
        + lede
        + views
        + footer
        + "</div>"
        + f"<script>{_JS}</script>"
        + "</body></html>\n"
    )


def write_board(site_dir, community_dir=None, gateway_dirs=None):
    """Build and write ``index.html`` + ``board.json`` under ``site_dir``."""
    site_dir = os.path.abspath(site_dir)
    doc = build_board(site_dir, community_dir=community_dir, gateway_dirs=gateway_dirs)
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
        "gateway_bundles": doc["gateway"]["bundle_count"],
        "skipped": len(doc["harness"]["skipped"]) + len(doc["gateway"]["skipped"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench site",
        description="Build the unified static leaderboard site (harness + gateway).",
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
        "--gateway-dir",
        action="append",
        default=None,
        help="gateway bundle root (repeatable; default: <site-dir>/gateway)",
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
            gateway_dirs=args.gateway_dir,
        )
        print(f"index.html  {info['html_path']}")
        print(f"board.json  {info['json_path']}")
        print(
            f"harness_bundles={info['harness_bundles']} "
            f"gateway_bundles={info['gateway_bundles']} "
            f"skipped={info['skipped']}"
        )
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
