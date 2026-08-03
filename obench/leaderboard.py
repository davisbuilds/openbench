#!/usr/bin/env python3
"""Harness Bench aggregation over verified OpenBench publish bundles.

Aggregates ``results.jsonl`` bundles from the GitHub Pages site
(``docs/releases/*/``, ``docs/community/*/``) and optional ``data/community/*/``
into the ranked boards that :mod:`obench.site` renders. This module owns the
comparability rules; it no longer renders a page of its own.

Comparability rule (the product): never blend cells from different bundles
into one score. Each bundle is its own ranked table. Cross-bundle rankings
on different task sets are not comparable — see the methodology note.
Within a bundle, arms are ``(harness, model)`` and denominators prefer the
matched table from ``obench.stats`` when two or more arms are present.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tomllib
from collections import defaultdict

from . import publish, stats
from .paths import SOURCE_ROOT

METHODOLOGY_NOTE = (
    "Each board below is one result-sealed publish bundle. Scores are never mixed "
    "across bundles: different task sets, trial counts, timeout caps, or run "
    "conditions make cross-bundle rankings non-comparable. Within a bundle, "
    "arms are (harness, model) and (when two or more arms exist) use matched "
    "(task, trial) denominators — the same philosophy as `obench compare`."
)

_CSS = (
    "body{font:16px/1.5 system-ui,sans-serif;color:#17202a;max-width:1200px;"
    "margin:auto;padding:2rem;background:#f7f8fa}"
    "header{padding:2rem;background:#18253b;color:white;border-radius:12px}"
    "section{margin:2rem 0;background:white;padding:1.4rem;border-radius:10px;"
    "box-shadow:0 1px 4px #ccd}h1{margin:.1rem 0}h2{margin:.2rem 0 .8rem}"
    ".scroll{overflow:auto}table{font-variant-numeric:tabular-nums;"
    "border-collapse:collapse;width:100%}"
    "th,td{padding:.65rem;border-bottom:1px solid #dde2e8;text-align:right;"
    "white-space:nowrap}th:first-child,td:first-child{text-align:left}"
    "thead th{background:#edf2f7}"
    ".warning{background:#fff1d6;border-left:4px solid #b45309;padding:.75rem}"
    ".note{background:#eef6ff;border-left:4px solid #075985;padding:.75rem}"
    ".tag{font-size:.85rem;color:#52606d}footer{color:#52606d;text-align:center}"
    ".caveat-flag{color:#b45309;font-weight:600}"
    "a{color:#075985}"
    "@media(max-width:700px){body{padding:.6rem}section,header{padding:1rem}}"
)

_CAVEATS_SECTION_RE = re.compile(
    r'id=["\']caveats["\']', re.IGNORECASE,
)
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _rel_under(root, path):
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    try:
        return os.path.relpath(path, root).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def _load_manifest_list(path):
    if not os.path.isfile(path):
        return []
    data = _read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return data


FINAL_RELEASE_STATUS = "final"
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def load_final_releases(site_dir):
    """Load and validate the exact set of finalized public release dirs.

    ``docs/releases`` is a publication boundary, not a discovery root. Every
    directory must be listed and every listed entry must explicitly opt in with
    ``status: final``. Any ambiguity fails the whole public-site build.
    """
    site_dir = os.path.abspath(site_dir)
    manifest_path = os.path.join(site_dir, "releases.json")
    releases = _load_manifest_list(manifest_path)
    by_id = {}
    for index, entry in enumerate(releases):
        if not isinstance(entry, dict):
            raise ValueError(
                f"{manifest_path} entry {index} must contain a JSON object"
            )
        release_id = entry.get("id")
        if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
            raise ValueError(
                f"{manifest_path} entry {index} has an invalid release id"
            )
        if release_id in by_id:
            raise ValueError(f"duplicate release id in {manifest_path}: {release_id}")
        status = entry.get("status")
        if status != FINAL_RELEASE_STATUS:
            shown = "missing" if status is None else repr(status)
            raise ValueError(
                f"release {release_id!r} has publication status {shown}; "
                f"public releases require status {FINAL_RELEASE_STATUS!r}"
            )
        expected_path = f"releases/{release_id}/index.html"
        if entry.get("path") != expected_path:
            raise ValueError(
                f"final release {release_id!r} must use canonical path "
                f"{expected_path!r}"
            )
        by_id[release_id] = entry

    releases_dir = os.path.join(site_dir, "releases")
    present = set()
    if os.path.exists(releases_dir):
        if not os.path.isdir(releases_dir) or os.path.islink(releases_dir):
            raise ValueError(f"public release root must be a directory: {releases_dir}")
        for item in os.scandir(releases_dir):
            if item.is_symlink() or not item.is_dir(follow_symlinks=False):
                raise ValueError(
                    f"unexpected non-directory entry under public release root: {item.path}"
                )
            present.add(item.name)

    unlisted = sorted(present - set(by_id))
    if unlisted:
        raise ValueError(
            "unlisted public release director"
            + ("y" if len(unlisted) == 1 else "ies")
            + f" under {releases_dir}: {', '.join(unlisted)}"
        )
    missing = sorted(set(by_id) - present)
    if missing:
        raise ValueError(
            "final release director"
            + ("y is" if len(missing) == 1 else "ies are")
            + f" missing under {releases_dir}: {', '.join(missing)}"
        )
    for release_id in by_id:
        index_path = os.path.join(releases_dir, release_id, "index.html")
        if os.path.islink(index_path) or not os.path.isfile(index_path):
            raise ValueError(
                f"final release {release_id!r} is missing its canonical "
                f"regular file: {index_path}"
            )
    return releases



def _default_tasks_dirs(site_dir):
    """Resolve source-backed task roots for repository leaderboard builds."""
    repo_root = os.path.dirname(os.path.abspath(site_dir))
    candidates = [
        os.path.join(repo_root, "tasks"),
        os.path.join(repo_root, "tasks-imported", "terminal-bench"),
        *stats.DEFAULT_TASK_DIRS,
    ]
    roots = []
    seen = set()
    for path in candidates:
        normalized = os.path.realpath(path)
        if os.path.isdir(normalized) and normalized not in seen:
            roots.append(normalized)
            seen.add(normalized)
    return roots


def _bundle_verification(bundle_dir, tasks_dirs):
    """Return a hard integrity error and current-tree archive drift checks."""

    results_path = os.path.join(bundle_dir, "results.jsonl")
    provenance_path = os.path.join(bundle_dir, "provenance.json")
    if not os.path.isfile(results_path):
        return "no results.jsonl (HTML-only release page)", []
    if not os.path.isfile(provenance_path):
        return "missing provenance.json (results are not verified)", []
    checks = publish.verify_bundle(
        bundle_dir, tasks_dirs=tasks_dirs, verify_task_trees=True)
    failed = [item for item in checks if item["status"] != "PASS"]
    archive_drift = [
        item for item in failed
        if item["name"].startswith(("task_digest:", "harbor_export_binding:"))
    ]
    hard_failures = [item for item in failed if item not in archive_drift]
    if hard_failures:
        return (
            "; ".join(
                f"{item['name']}: {item['detail']}"
                for item in hard_failures
            ),
            archive_drift,
        )
    return None, archive_drift


def _results_verification_error(bundle_dir, tasks_dirs):
    """Return why a result bundle fails its immutable evidence checks."""

    error, _archive_drift = _bundle_verification(bundle_dir, tasks_dirs)
    return error


def _archive_drift_caveat(checks):
    tasks = sorted({
        item["name"].split(":", 1)[1]
        for item in checks
        if ":" in item["name"]
    })
    if not tasks:
        return None
    return (
        "Archived task definitions differ from or are unavailable in the "
        f"current checkout for {', '.join(tasks)}. The result seal and bundled "
        "evidence still verify; recorded task digests remain the run-time "
        "fingerprints."
    )


def task_set_digest(provenance):
    """Stable digest over provenance task content digests (sorted by task)."""
    if not isinstance(provenance, dict):
        return None
    tasks = provenance.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return None
    lines = []
    for item in sorted(tasks, key=lambda t: str((t or {}).get("task") or "")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("task") or "")
        digest = str(item.get("content_digest") or "")
        if name and digest:
            lines.append(f"{name}:{digest}")
    if not lines:
        return None
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def load_bundle_caveats(bundle_dir):
    """Return caveat strings from leaderboard.toml and/or index.html."""
    caveats = []
    toml_path = os.path.join(bundle_dir, "leaderboard.toml")
    if os.path.isfile(toml_path):
        with open(toml_path, "rb") as fh:
            raw = tomllib.load(fh)
        if isinstance(raw, dict):
            listed = raw.get("caveats") or []
            if isinstance(listed, list):
                for item in listed:
                    if isinstance(item, str) and item.strip():
                        caveats.append(item.strip())
    html_path = os.path.join(bundle_dir, "index.html")
    if os.path.isfile(html_path):
        with open(html_path, encoding="utf-8") as fh:
            page = fh.read()
        if _CAVEATS_SECTION_RE.search(page):
            # Prefer <li> items inside the caveats section when present.
            start = _CAVEATS_SECTION_RE.search(page).start()
            window = page[start:start + 4000]
            for match in _LI_RE.finditer(window):
                text = _TAG_RE.sub("", match.group(1))
                text = html.unescape(re.sub(r"\s+", " ", text)).strip()
                if text and text not in caveats:
                    caveats.append(text)
            if not caveats:
                caveats.append(
                    "This bundle discloses caveats on its release page; "
                    "inspect the source card before comparing arms."
                )
    return caveats


def _bundle_meta(bundle_dir, kind, manifest_entry=None):
    """Title/date/models/path for a discovered bundle directory."""
    entry = dict(manifest_entry or {})
    title = entry.get("title")
    date_s = entry.get("date")
    models = list(entry.get("models") or [])
    page_rel = entry.get("path")
    claim = entry.get("claim")
    submitter = entry.get("submitter")
    link = entry.get("link")

    prov_path = os.path.join(bundle_dir, "provenance.json")
    provenance = {}
    if os.path.isfile(prov_path):
        try:
            loaded = _read_json(prov_path)
            if isinstance(loaded, dict):
                provenance = loaded
        except (OSError, json.JSONDecodeError):
            provenance = {}

    if not models and isinstance(provenance.get("models"), list):
        models = [str(m) for m in provenance["models"] if m]

    sub_path = os.path.join(bundle_dir, "submission.toml")
    if os.path.isfile(sub_path) and (not title or not date_s):
        try:
            with open(sub_path, "rb") as fh:
                sub = tomllib.load(fh)
            if isinstance(sub, dict):
                title = title or sub.get("title") or sub.get("claim")
                date_s = date_s or sub.get("date")
                claim = claim or sub.get("claim")
                submitter = submitter or sub.get("submitter")
                link = link if link is not None else sub.get("link")
        except (OSError, tomllib.TOMLDecodeError):
            pass

    bid = entry.get("id") or os.path.basename(os.path.normpath(bundle_dir))
    if not title:
        title = bid
    if not isinstance(date_s, str):
        date_s = ""
    return {
        "id": bid,
        "kind": kind,
        "title": str(title),
        "date": date_s,
        "models": sorted(str(m) for m in models),
        "claim": claim,
        "submitter": submitter,
        "link": link,
        "page_path": page_rel,
        "provenance": provenance,
    }


def discover_bundle_dirs(site_dir, community_dir=None):
    """Yield candidate bundle dirs as ``(kind, abs_dir, manifest_entry|None)``.

    Order is deterministic: finalized releases in manifest order, then site
    community, then optional data/community leftovers.
    """
    site_dir = os.path.abspath(site_dir)
    seen_real = set()

    def _emit(kind, parent, manifest_by_id):
        parent = os.path.join(site_dir, parent) if not os.path.isabs(parent) else parent
        if not os.path.isdir(parent):
            return
        names = []
        for entry_id in manifest_by_id:
            path = os.path.join(parent, entry_id)
            if os.path.isdir(path):
                names.append(entry_id)
        for name in sorted(os.listdir(parent)):
            if name in names:
                continue
            if os.path.isdir(os.path.join(parent, name)):
                names.append(name)
        for name in names:
            path = os.path.join(parent, name)
            real = os.path.realpath(path)
            if real in seen_real:
                continue
            seen_real.add(real)
            yield kind, path, manifest_by_id.get(name)

    releases_manifest = {e["id"]: e for e in load_final_releases(site_dir)}
    for release_id, entry in releases_manifest.items():
        path = os.path.join(site_dir, "releases", release_id)
        real = os.path.realpath(path)
        seen_real.add(real)
        yield "release", path, entry

    community_manifest = {
        e["id"]: e for e in _load_manifest_list(os.path.join(site_dir, "community.json"))
        if isinstance(e, dict) and e.get("id")
    }
    yield from _emit("community", "community", community_manifest)

    if community_dir:
        community_dir = os.path.abspath(community_dir)
        if os.path.isdir(community_dir):
            # Treat data/community as community kind; no site-relative page path.
            data_manifest = {}
            for name in sorted(os.listdir(community_dir)):
                path = os.path.join(community_dir, name)
                if not os.path.isdir(path):
                    continue
                real = os.path.realpath(path)
                if real in seen_real:
                    continue
                seen_real.add(real)
                yield "community", path, data_manifest.get(name)


TOKEN_SPLITS = (
    "input_uncached",
    "output",
    "cache_read",
    "cache_write",
)
TOKEN_CORE_SPLITS = (
    "input_uncached",
    "output",
    "cache_read",
)


def _token_lane(rows, source):
    """Return complete rows and exact bases for one telemetry source."""
    prefix = "tokens_proxy_" if source == "proxy" else "tokens_"
    complete = []
    bases = set()
    for row in rows:
        values = {split: row.get(prefix + split) for split in TOKEN_SPLITS}
        measured = (
            row.get("token_basis_proxy") == "proxy_measured"
            if source == "proxy"
            else True
        )
        if measured and all(
            stats.is_nonnegative_number(values[split])
            for split in TOKEN_CORE_SPLITS
        ):
            complete.append(values)
            raw_basis = (
                row.get("token_basis_proxy")
                if source == "proxy"
                else row.get("token_basis")
            )
            bases.add(str(raw_basis) if raw_basis not in (None, "") else "unknown")
    return complete, sorted(bases)


def token_telemetry_per_solve(rows, solved=None):
    """Select one complete split lane for an arm and aggregate its fields."""
    if solved is None:
        solved = sum(bool(row.get("success")) for row in rows)
    total_rows = len(rows)
    proxy_rows, proxy_bases = _token_lane(rows, "proxy")
    native_rows, native_bases = _token_lane(rows, "native")
    coverage = {
        "total_rows": total_rows,
        "proxy_covered_rows": len(proxy_rows),
        "native_covered_rows": len(native_rows),
    }

    source = None
    lane = []
    bases = []
    if total_rows and len(proxy_rows) == total_rows:
        source, lane, bases = "proxy", proxy_rows, proxy_bases
    elif total_rows and len(native_rows) == total_rows:
        source, lane, bases = "native", native_rows, native_bases

    selected_rows = len(lane)
    coverage.update({
        "covered_rows": selected_rows,
        "ratio": selected_rows / total_rows if total_rows else None,
    })
    metrics = {
        "fresh_tokens_per_solve": None,
        "tokens_input_uncached_per_solve": None,
        "tokens_output_per_solve": None,
        "tokens_cache_read_per_solve": None,
        "tokens_cache_write_per_solve": None,
    }
    if source is not None and solved:
        sums = {
            split: sum(float(values[split]) for values in lane)
            for split in TOKEN_CORE_SPLITS
        }
        cache_write_values = [values["cache_write"] for values in lane]
        cache_write_sum = (
            sum(float(value) for value in cache_write_values)
            if all(
                stats.is_nonnegative_number(value)
                for value in cache_write_values
            )
            else None
        )
        metrics.update({
            "fresh_tokens_per_solve":
                (sums["input_uncached"] + sums["output"]) / solved,
            "tokens_input_uncached_per_solve": sums["input_uncached"] / solved,
            "tokens_output_per_solve": sums["output"] / solved,
            "tokens_cache_read_per_solve": sums["cache_read"] / solved,
            "tokens_cache_write_per_solve": (
                cache_write_sum / solved
                if cache_write_sum is not None
                else None
            ),
        })
    return {
        **metrics,
        "token_telemetry_source": source,
        "token_telemetry_bases": bases,
        "token_telemetry_coverage": coverage,
    }


def aggregate_bundle(
    bundle_dir, *, kind, manifest_entry=None, site_dir=None, tasks_dirs=None,
):
    """Aggregate one bundle. Returns None when results.jsonl is missing."""
    results_path = os.path.join(bundle_dir, "results.jsonl")
    if tasks_dirs is None:
        inferred_site = site_dir or os.path.join(
            os.path.dirname(bundle_dir), "docs"
        )
        tasks_dirs = _default_tasks_dirs(inferred_site)
    verification_error, archive_drift = _bundle_verification(
        bundle_dir, tasks_dirs
    )
    if verification_error is not None:
        return None

    meta = _bundle_meta(bundle_dir, kind, manifest_entry=manifest_entry)
    provenance = meta["provenance"]
    rows = stats.load_rows([results_path])
    # Published scores must not shift with local DROPPED.md overlays.
    filtered = stats.filter_rows(rows, [])
    countable = filtered["countable_rows"]
    fields = ("harness", "model")
    mrows, mdiag = stats.matched_rows(countable, fields)
    if mdiag is not None:
        table_rows = mrows
        table_name = "matched"
        matched = mdiag
    else:
        table_rows = countable
        table_name = "all_countable"
        matched = None

    by_arm = defaultdict(list)
    for row in table_rows:
        key = (str(row.get("harness") or "-"), str(row.get("model") or "-"))
        by_arm[key].append(row)

    arms = []
    for harness, model in sorted(by_arm):
        arm_rows = by_arm[(harness, model)]
        solved = sum(1 for r in arm_rows if r.get("success"))
        n = len(arm_rows)
        lo, hi = stats.wilson_ci(solved, n)
        rate = (solved / n) if n else None
        token_telemetry = token_telemetry_per_solve(arm_rows, solved)
        arms.append({
            "harness": harness,
            "model": model,
            "solved": solved,
            "n": n,
            "solve_rate": rate,
            "wilson95": [lo, hi],
            **token_telemetry,
        })

    # Rank within bundle: rate desc, Wilson lo desc, harness, model.
    arms.sort(
        key=lambda a: (
            -(a["solve_rate"] if a["solve_rate"] is not None else -1.0),
            -(a["wilson95"][0] if a["wilson95"] else 0.0),
            a["harness"],
            a["model"],
        )
    )

    caveats = load_bundle_caveats(bundle_dir)
    drift_caveat = _archive_drift_caveat(archive_drift)
    if drift_caveat and drift_caveat not in caveats:
        caveats.append(drift_caveat)
    results_sha = provenance["results_sha256"]

    page_path = meta["page_path"]
    if not page_path and site_dir:
        index_html = os.path.join(bundle_dir, "index.html")
        if os.path.isfile(index_html):
            page_path = _rel_under(site_dir, index_html)
    results_rel = _rel_under(site_dir, results_path) if site_dir else results_path

    return {
        "id": meta["id"],
        "kind": kind,
        "title": meta["title"],
        "date": meta["date"],
        "models": meta["models"],
        "claim": meta["claim"],
        "submitter": meta["submitter"],
        "link": meta["link"],
        "path": page_path,
        "results_path": results_rel.replace(os.sep, "/"),
        "results_sha256": results_sha,
        "task_set_digest": task_set_digest(provenance),
        "table": table_name,
        "matched": matched,
        "raw_rows": len(rows),
        "countable_rows": len(countable),
        "excluded_counts": filtered["excluded_counts"],
        "caveats": caveats,
        "has_caveats": bool(caveats),
        "arms": arms,
        "also_seen_as": [],
    }


def build_leaderboard(site_dir, community_dir=None, tasks_dirs=None):
    """Scan bundles and return the deterministic leaderboard document."""
    site_dir = os.path.abspath(site_dir)
    if tasks_dirs is None:
        tasks_dirs = _default_tasks_dirs(site_dir)
    else:
        tasks_dirs = stats.parse_tasks_dirs(tasks_dirs)
    bundles = []
    skipped = []
    by_sha = {}

    # HTML-only releases from the manifest (no results.jsonl).
    for entry in load_final_releases(site_dir):
        bundle_dir = os.path.join(site_dir, "releases", entry["id"])
        verification_error = _results_verification_error(bundle_dir, tasks_dirs)
        if verification_error:
            skipped.append({
                "id": entry["id"],
                "kind": "release",
                "reason": verification_error,
                "path": entry.get("path"),
            })

    for kind, bundle_dir, manifest_entry in discover_bundle_dirs(
        site_dir, community_dir=community_dir
    ):
        verification_error = _results_verification_error(bundle_dir, tasks_dirs)
        if verification_error:
            bundle_id = ((manifest_entry or {}).get("id")
                         or os.path.basename(bundle_dir))
            if not any(item.get("id") == bundle_id for item in skipped):
                skipped.append({
                    "id": bundle_id,
                    "kind": kind,
                    "reason": verification_error,
                    "path": (manifest_entry or {}).get("path"),
                })
            continue
        bundled = aggregate_bundle(
            bundle_dir, kind=kind, manifest_entry=manifest_entry, site_dir=site_dir,
            tasks_dirs=tasks_dirs,
        )
        if bundled is None:
            continue
        sha = bundled["results_sha256"]
        if sha in by_sha:
            primary = by_sha[sha]
            seen_alias_ids = {a.get("id") for a in primary["also_seen_as"]}
            if bundled["id"] not in seen_alias_ids and bundled["id"] != primary["id"]:
                primary["also_seen_as"].append({
                    "id": bundled["id"],
                    "kind": bundled["kind"],
                    "path": bundled.get("path"),
                    "results_path": bundled.get("results_path"),
                })
            continue
        by_sha[sha] = bundled
        bundles.append(bundled)

    # Date descending, then id ascending (stable regeneration diffs).
    bundles.sort(key=lambda b: (-_date_key(b.get("date")), b.get("id") or ""))

    skipped.sort(key=lambda s: (s.get("kind") or "", s.get("id") or ""))

    return {
        "generated_by": "obench leaderboard",
        "methodology_note": METHODOLOGY_NOTE,
        "bundle_count": len(bundles),
        "bundles": bundles,
        "skipped": skipped,
    }


def _date_key(value):
    if not value:
        return 0
    try:
        parts = str(value).split("-")
        return int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])
    except (IndexError, ValueError):
        return 0


def _fmt_pct(rate):
    if rate is None:
        return "—"
    return f"{rate * 100:.1f}%"


def _fmt_wilson(bounds):
    if not bounds or len(bounds) != 2:
        return "—"
    return f"{bounds[0] * 100:.1f}–{bounds[1] * 100:.1f}%"


def _fmt_tokens(value):
    if value is None:
        return "—"
    value = float(value)
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.1f}"


def write_leaderboard(site_dir, community_dir=None):
    """Deprecated shim: the leaderboard is now the site landing page.

    Kept so existing scripts keep working; it builds the same artifacts as
    ``obench site build``.
    """
    from . import site
    info = site.write_board(site_dir, community_dir=community_dir)
    return {
        "json_path": info["json_path"],
        "html_path": info["html_path"],
        "index_path": info["html_path"],
        "bundle_count": info["harness_bundles"],
        "skipped_count": info["skipped"],
    }


def _default_site_dir():
    root = SOURCE_ROOT if os.path.isdir(os.path.join(SOURCE_ROOT, "docs")) else os.getcwd()
    return os.path.join(root, "docs")


def _default_community_dir():
    root = SOURCE_ROOT if os.path.isdir(os.path.join(SOURCE_ROOT, "data")) else os.getcwd()
    path = os.path.join(root, "data", "community")
    return path if os.path.isdir(path) else None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench leaderboard",
        description="Build the static verified-bundle leaderboard for the docs site.",
    )
    sub = parser.add_subparsers(dest="command")
    build = sub.add_parser(
        "build",
        help="alias for `obench site build` (kept for existing scripts)",
    )
    build.add_argument(
        "--site-dir",
        default=_default_site_dir(),
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
        help="do not scan data/community (only site-dir releases + community)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "build":
        community_dir = args.community_dir
        if community_dir is None and not args.no_community_dir:
            community_dir = _default_community_dir()
        if args.no_community_dir:
            community_dir = None
        info = write_leaderboard(
            args.site_dir,
            community_dir=community_dir,
        )
        print("note: `obench leaderboard build` is now `obench site build`")
        print(f"index.html  {info['html_path']}")
        print(f"board.json  {info['json_path']}")
        print(f"bundles={info['bundle_count']} skipped={info['skipped_count']}")
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
