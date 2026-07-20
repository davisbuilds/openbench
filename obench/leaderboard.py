#!/usr/bin/env python3
"""Static leaderboard for verified OpenBench publish bundles.

Aggregates ``results.jsonl`` bundles from the GitHub Pages site
(``docs/releases/*/``, ``docs/community/*/``) and optional
``data/community/*/`` into ``docs/leaderboard.html`` + ``leaderboard.json``.

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

from . import stats
from .paths import SOURCE_ROOT

METHODOLOGY_NOTE = (
    "Each board below is one verified publish bundle. Scores are never mixed "
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

    Order is deterministic: releases (manifest order, then leftover dirs
    sorted), then site community, then optional data/community leftovers.
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

    releases_manifest = {
        e["id"]: e for e in _load_manifest_list(os.path.join(site_dir, "releases.json"))
        if isinstance(e, dict) and e.get("id")
    }
    yield from _emit("release", "releases", releases_manifest)

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


def _effective_tokens_per_solve(rows):
    """Mean effective tokens among solved rows that reported tokens; basis tags."""
    token_vals = []
    bases = set()
    solved = 0
    for row in rows:
        if not row.get("success"):
            continue
        solved += 1
        tok, basis = stats.effective_tokens(row)
        if tok is not None:
            token_vals.append(float(tok))
            if basis:
                bases.add(basis)
    if solved <= 0 or not token_vals:
        return None, sorted(bases)
    # Match report.py: total reported tokens / solves (not mean of only metered).
    return sum(token_vals) / solved, sorted(bases)


def aggregate_bundle(bundle_dir, *, kind, manifest_entry=None, site_dir=None):
    """Aggregate one bundle. Returns None when results.jsonl is missing."""
    results_path = os.path.join(bundle_dir, "results.jsonl")
    if not os.path.isfile(results_path):
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
        tok_slv, bases = _effective_tokens_per_solve(arm_rows)
        arms.append({
            "harness": harness,
            "model": model,
            "solved": solved,
            "n": n,
            "solve_rate": rate,
            "wilson95": [lo, hi],
            "effective_tokens_per_solve": tok_slv,
            "token_bases": bases,
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
    results_sha = provenance.get("results_sha256")
    if not isinstance(results_sha, str) or not results_sha:
        # Fall back to hashing the file so dedupe still works.
        h = hashlib.sha256()
        with open(results_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        results_sha = h.hexdigest()

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


def build_leaderboard(site_dir, community_dir=None):
    """Scan bundles and return the deterministic leaderboard document."""
    site_dir = os.path.abspath(site_dir)
    bundles = []
    skipped = []
    by_sha = {}

    # HTML-only releases from the manifest (no results.jsonl).
    for entry in _load_manifest_list(os.path.join(site_dir, "releases.json")):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        results = os.path.join(site_dir, "releases", entry["id"], "results.jsonl")
        if not os.path.isfile(results):
            skipped.append({
                "id": entry["id"],
                "kind": "release",
                "reason": "no results.jsonl (HTML-only release page)",
                "path": entry.get("path"),
            })

    for kind, bundle_dir, manifest_entry in discover_bundle_dirs(
        site_dir, community_dir=community_dir
    ):
        bundled = aggregate_bundle(
            bundle_dir, kind=kind, manifest_entry=manifest_entry, site_dir=site_dir,
        )
        if bundled is None:
            # Already recorded HTML-only releases above; skip quiet dirs.
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


def render_leaderboard_html(doc):
    """Self-contained static HTML matching the release-page visual language."""
    sections = []
    sections.append(
        '<section class="note" id="methodology"><h2>Comparability</h2><p>'
        + html.escape(doc["methodology_note"])
        + "</p><p class=\"tag\">Ranks are per bundle only. There is no global "
        "cross-bundle harness ranking on this page.</p></section>"
    )

    for bundle in doc.get("bundles") or []:
        title = html.escape(bundle.get("title") or bundle.get("id") or "")
        kind = html.escape(bundle.get("kind") or "")
        date_s = html.escape(bundle.get("date") or "")
        table = html.escape(bundle.get("table") or "")
        models = html.escape(", ".join(bundle.get("models") or []) or "—")
        links = []
        if bundle.get("path"):
            links.append(
                f'<a href="{html.escape(bundle["path"], quote=True)}">bundle card</a>'
            )
        if bundle.get("results_path"):
            links.append(
                f'<a href="{html.escape(bundle["results_path"], quote=True)}">'
                "results.jsonl</a>"
            )
        for alias in bundle.get("also_seen_as") or []:
            label = f'{alias.get("kind")}/{alias.get("id")}'
            if alias.get("path"):
                links.append(
                    f'<a href="{html.escape(alias["path"], quote=True)}">'
                    + html.escape(label) + "</a>"
                )
            else:
                links.append(html.escape(label))
        link_html = " · ".join(links) if links else ""

        caveat_block = ""
        if bundle.get("has_caveats"):
            items = "".join(
                f"<li>{html.escape(c)}</li>" for c in (bundle.get("caveats") or [])
            )
            caveat_block = (
                '<p class="warning"><strong class="caveat-flag">Caveats</strong>'
                f"<ul>{items}</ul></p>"
            )

        digest = bundle.get("task_set_digest") or "—"
        meta = (
            f'<p class="tag">{kind} · {date_s} · model(s): {models} · '
            f"table: {table} · task_set_digest: "
            f"<code>{html.escape(str(digest)[:16])}…</code>"
            + (f" · {link_html}" if link_html else "")
            + "</p>"
        )

        rows_html = []
        for rank, arm in enumerate(bundle.get("arms") or [], 1):
            bases = ", ".join(arm.get("token_bases") or []) or "—"
            caveat_cell = (
                '<span class="caveat-flag">yes</span>'
                if bundle.get("has_caveats") else "—"
            )
            rows_html.append(
                "<tr>"
                f"<td>{rank}</td>"
                f"<td>{html.escape(arm.get('harness') or '')} × "
                f"{html.escape(arm.get('model') or '')}</td>"
                f"<td>{arm.get('solved', 0)}/{arm.get('n', 0)}</td>"
                f"<td>{html.escape(_fmt_pct(arm.get('solve_rate')))}</td>"
                f"<td>{html.escape(_fmt_wilson(arm.get('wilson95')))}</td>"
                f"<td>{html.escape(_fmt_tokens(arm.get('effective_tokens_per_solve')))}</td>"
                f"<td>{html.escape(bases)}</td>"
                f"<td>{caveat_cell}</td>"
                "</tr>"
            )
        body = "".join(rows_html) or (
            "<tr><td colspan=\"8\">No countable arms in this bundle.</td></tr>"
        )
        table_html = (
            '<div class="scroll"><table><thead><tr>'
            "<th>#</th><th>Arm (harness × model)</th><th>Solved/n</th>"
            "<th>Solve rate</th><th>Wilson 95% CI</th>"
            "<th>Eff. tokens/solve</th><th>Token basis</th><th>Caveat</th>"
            "</tr></thead><tbody>" + body + "</tbody></table></div>"
        )
        sections.append(
            f"<section id=\"{html.escape(bundle.get('id') or '', quote=True)}\">"
            f"<h2>{title}</h2>{meta}{caveat_block}{table_html}</section>"
        )

    if doc.get("skipped"):
        items = "".join(
            "<li><code>" + html.escape(s.get("id") or "") + "</code> — "
            + html.escape(s.get("reason") or "") + "</li>"
            for s in doc["skipped"]
        )
        sections.append(
            "<section><h2>Skipped (no machine-readable bundle)</h2>"
            "<p class=\"tag\">These release pages have HTML cards but no "
            "<code>results.jsonl</code>, so they cannot appear in the ranked "
            f"tables.</p><ul>{items}</ul></section>"
        )

    if not (doc.get("bundles") or []):
        sections.append(
            "<section><p>No verified bundles with "
            "<code>results.jsonl</code> were found.</p></section>"
        )

    n = doc.get("bundle_count", 0)
    headline = (
        f"{n} verified bundle{'s' if n != 1 else ''} with machine-readable "
        "results · ranks are per bundle only"
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>OpenBench leaderboard</title><style>"
        + _CSS
        + "</style></head><body><header><div class=\"tag\">OPENBENCH</div>"
        "<h1>Leaderboard</h1><p>"
        + html.escape(headline)
        + "</p></header>"
        + "".join(sections)
        + "<footer>Generated by OpenBench · static, self-contained HTML · "
        "<a href=\"index.html\">all releases</a></footer></body></html>\n"
    )


def write_leaderboard(site_dir, community_dir=None, *, refresh_index=True):
    """Build and write ``leaderboard.json`` + ``leaderboard.html`` under site_dir."""
    site_dir = os.path.abspath(site_dir)
    doc = build_leaderboard(site_dir, community_dir=community_dir)
    json_path = os.path.join(site_dir, "leaderboard.json")
    html_path = os.path.join(site_dir, "leaderboard.html")
    json_text = json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    _write_text(json_path, json_text)
    _write_text(html_path, render_leaderboard_html(doc))
    index_path = None
    if refresh_index:
        from .community import write_site_index
        index_path = write_site_index(site_dir)
    return {
        "json_path": json_path,
        "html_path": html_path,
        "index_path": index_path,
        "bundle_count": doc["bundle_count"],
        "skipped_count": len(doc.get("skipped") or []),
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
        help="scan release/community bundles and write leaderboard.html + .json",
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
    build.add_argument(
        "--no-refresh-index",
        action="store_true",
        help="do not regenerate docs/index.html",
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
            refresh_index=not args.no_refresh_index,
        )
        print(f"leaderboard.html  {info['html_path']}")
        print(f"leaderboard.json  {info['json_path']}")
        if info.get("index_path"):
            print(f"index.html        {info['index_path']}")
        print(
            f"bundles={info['bundle_count']} "
            f"skipped={info['skipped_count']}"
        )
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
